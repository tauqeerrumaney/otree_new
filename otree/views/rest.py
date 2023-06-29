import json

from django.conf import settings
from django.http import HttpResponse, HttpResponseNotFound, JsonResponse
from django.shortcuts import reverse, get_object_or_404
from django.template.loader import get_template


import otree
import otree.bots.browser
from otree.channels import utils as channel_utils
from otree.models import Session
from otree.models_concrete import (
    ParticipantVarsFromREST,
    BrowserBotsLauncherSessionCode,
)
from otree.room import ROOM_DICT
from otree.session import create_session, SESSION_CONFIGS_DICT
from otree.views.abstract import BaseRESTView
from otree.common import json_dumps


class PostParticipantVarsThroughREST(BaseRESTView):

    url_pattern = r'^api/participant_vars/$'

    def inner_post(self, room_name, participant_label, vars):
        if room_name not in ROOM_DICT:
            return HttpResponseNotFound(f'Room {room_name} not found')
        room = ROOM_DICT[room_name]
        session = room.get_session()
        if session:
            participant = session.participant_set.filter(
                label=participant_label
            ).first()
            if participant:
                participant.vars.update(vars)
                participant.save()
                return HttpResponse('ok')
        obj, _ = ParticipantVarsFromREST.objects.update_or_create(
            participant_label=participant_label,
            room_name=room_name,
            defaults=dict(_json_data=json.dumps(vars)),
        )
        return JsonResponse({})


class RESTSessionVars(BaseRESTView):
    url_pattern = r'^api/session_vars/$'

    def inner_post(self, room_name, vars):
        if room_name not in ROOM_DICT:
            return HttpResponseNotFound(f'Room {room_name} not found')
        room = ROOM_DICT[room_name]
        session = room.get_session()
        if not session:
            return HttpResponseNotFound(f'No current session in room {room_name}')
        session.vars.update(vars)
        session.save()
        return JsonResponse({})


def get_session_urls(session: Session, request) -> dict:
    build_uri = request.build_absolute_uri
    d = dict(
        session_wide_url=build_uri(
            reverse(
                'JoinSessionAnonymously',
                kwargs=dict(anonymous_code=session._anonymous_code),
            )
        ),
        admin_url=build_uri(
            reverse('SessionStartLinks', kwargs=dict(code=session.code))
        ),
    )
    room = session.get_room()
    if room:
        d['room_url'] = room.get_room_wide_url(request)
    return d


class RESTCreateSession(BaseRESTView):

    url_pattern = r'^api/sessions/$'

    def inner_post(self, **kwargs):
        '''
        Notes:
        - This allows you to pass parameters that did not exist in the original config,
        as well as params that are blocked from editing in the UI,
        either because of datatype.
        I can't see any specific problem with this.
        '''
        session = create_session(**kwargs)
        room_name = kwargs.get('room_name')
        if room_name:
            channel_utils.sync_group_send_wrapper(
                type='room_session_ready',
                group=channel_utils.room_participants_group_name(room_name),
                event={},
            )

        response_payload = dict(
            code=session.code, **get_session_urls(session, self.request)
        )

        return JsonResponse(response_payload)


class RESTGetSessionInfo(BaseRESTView):
    url_pattern = 'api/session'

    def inner_get(self, code, participant_labels=None):
        session = get_object_or_404(Session, code=code)
        pp_set = session.participant_set
        if participant_labels is not None:
            pp_set = pp_set.filter(label__in=participant_labels)
        pdata = []
        for id_in_session, code, label, payoff in pp_set.values_list(
            'id_in_session', 'code', 'label', 'payoff',
        ):
            pdata.append(
                dict(
                    id_in_session=id_in_session,
                    code=code,
                    label=label,
                    payoff_in_real_world_currency=payoff.to_real_world_currency(
                        session
                    ),
                )
            )

        payload = dict(
            # we need the session config for mturk settings and participation fee
            # technically, other parts of session config might not be JSON serializable
            config=session.config,
            num_participants=session.num_participants,
            REAL_WORLD_CURRENCY_CODE=settings.REAL_WORLD_CURRENCY_CODE,
            participants=pdata,
            **get_session_urls(session, self.request),
        )

        mturk_settings = session.config.get('mturk_hit_settings')
        if mturk_settings:
            # apparently the .template is needed
            payload['mturk_template_html'] = get_template(
                mturk_settings['template']
            ).template.source

        # need custom json_dumps for currency values
        return HttpResponse(json_dumps(payload))


class RESTSessionConfigs(BaseRESTView):
    url_pattern = 'api/session_configs'

    def inner_get(self):
        return HttpResponse(json_dumps(list(SESSION_CONFIGS_DICT.values())))


class RESTOTreeVersion(BaseRESTView):
    url_pattern = 'api/otree_version'

    def inner_get(self):
        return JsonResponse(dict(version=otree.__version__))


class RESTCreateSessionLegacy(RESTCreateSession):
    url_pattern = r'^api/v1/sessions/$'


class RESTSessionVarsLegacy(RESTSessionVars):
    url_pattern = r'^api/v1/session_vars/$'


class RESTParticipantVarsLegacy(PostParticipantVarsThroughREST):
    url_pattern = r'^api/v1/participant_vars/$'


class CreateBrowserBotsSession(BaseRESTView):
    url_pattern = r"^create_browser_bots_session/$"

    def inner_post(
        self, num_participants, session_config_name, case_number, pre_create_id
    ):
        session = create_session(
            session_config_name=session_config_name, num_participants=num_participants
        )
        otree.bots.browser.initialize_session(
            session_pk=session.pk, case_number=case_number
        )
        BrowserBotsLauncherSessionCode.objects.update_or_create(
            # i don't know why the update_or_create arg is called 'defaults'
            # because it will update even if the instance already exists
            # maybe for consistency with get_or_create
            defaults=dict(code=session.code, pre_create_id=pre_create_id)
        )
        channel_utils.sync_group_send_wrapper(
            type='browserbot_sessionready', group='browser_bot_wait', event={}
        )

        return HttpResponse(session.code)


class CloseBrowserBotsSession(BaseRESTView):
    url_pattern = r"^close_browser_bots_session/$"

    def inner_post(self, **kwargs):
        BrowserBotsLauncherSessionCode.objects.all().delete()
        return HttpResponse('ok')
