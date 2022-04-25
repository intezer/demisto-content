from enum import Enum
import urllib3
from CommonServerPython import *
import demistomock as demisto
from pydantic import BaseConfig, BaseModel, AnyUrl, Json, Field
import requests
from requests.auth import HTTPBasicAuth
import dateparser
from datetime import datetime

urllib3.disable_warnings()

DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"


class Method(str, Enum):
    GET = 'GET'
    POST = 'POST'
    PUT = 'PUT'
    HEAD = 'HEAD'
    PATCH = 'PATCH'
    DELETE = 'DELETE'


class Args(BaseModel):
    from_: str = Field(
        datetime.strftime(
            dateparser.parse(demisto.params().get('first_fetch', '3 days'), settings={'TIMEZONE': 'UTC'}),
            DATETIME_FORMAT
        ),
        alias='from'
    )
    limit: int = 1000
    offset: int = 0


class ReqParams(BaseModel):
    from_: str = Field(
        datetime.strftime(
            dateparser.parse(demisto.params().get('first_fetch', '3 days'), settings={'TIMEZONE': 'UTC'}),
            DATETIME_FORMAT
        ),
        alias='from'
    )
    limit: int = 1000
    offset: int = 0


class Request(BaseModel):
    method: Method
    url: AnyUrl
    headers: Union[Json, dict] = {}
    params: Optional[ReqParams]
    insecure: bool = Field(not demisto.params().get('insecure', False), alias='verify')
    proxy: bool = Field(demisto.params().get('proxy', False), alias='proxies')
    data: Optional[str]
    auth: Optional[HTTPBasicAuth] = Field(
        HTTPBasicAuth(
            demisto.params().get('credentials', {}).get('identifier'),
            demisto.params().get('credentials', {}).get('password')
        )
    )

    class Config(BaseConfig):
        arbitrary_types_allowed = True


class Client:
    def __init__(self, request: Request, session=requests.Session()):
        self.request = request
        self.session = session
        self._set_proxy()
        self._set_cert_verification()

    def __del__(self):
        try:
            self.session.close()
        except AttributeError as err:
            demisto.debug(f'Ignore exceptions raised due to session not used by the client. {err}')

    def call(self) -> requests.Response:
        try:
            response = self.session.request(**self.request.dict(by_alias=True))
            response.raise_for_status()
            return response
        except Exception as exc:
            msg = f'Something went wrong with the http call {exc}'
            LOG(msg)
            raise DemistoException(msg) from exc

    def prepare_next_run(self, offset: int):
        self.request.params.offset += offset

    def _set_cert_verification(self):
        if not self.request.insecure:
            skip_cert_verification()

    def _set_proxy(self):
        if self.request.proxy:
            ensure_proxy_has_http_prefix()
        else:
            skip_proxy()


class GetEvents:
    def __init__(self, client: Client) -> None:
        self.client = client

    def call(self) -> list:
        resp = self.client.call()
        return resp.json().get('records', [])

    def _iter_events(self):
        events = self.call()

        while events:
            yield events

            self.client.prepare_next_run(self.client.request.params.limit)
            events = self.call()

    def run(self, max_fetch: int = 100) -> list[dict]:
        stored = []
        last_run = demisto.getLastRun()

        for logs in self._iter_events():
            stored.extend(logs)

            if len(stored) > max_fetch:
                last_run['offset'] = last_run.get('offset', 0) + max_fetch
                demisto.setLastRun(last_run)
                return stored[:max_fetch]

        last_run['offset'] = 0
        demisto.setLastRun(last_run)
        return stored

    @staticmethod
    def events_to_incidents(events: list):
        incidents = []

        for event in events:
            incident = {
                'name': f"JiraSIEM - {event['summary']} - {event['id']}",
                'occurred': event.get('created', '').removesuffix('+0000') + 'Z',
                'rawJSON': json.dumps(event)
            }
            incidents.append(incident)

        demisto.incidents(incidents)

    @staticmethod
    def set_next_run(log: dict) -> dict:
        last_run = demisto.getLastRun()

        if not last_run.get('next_time'):
            last_time = log.get('created', '').removesuffix('+0000')
            next_time = last_time[:-1] + str(int(last_time[-1]) + 1)

            if last_run.get('offset'):
                last_run['next_time'] = next_time
            else:
                last_run['from'] = next_time

        else:
            if not last_run.get('offset'):
                last_run['from'] = last_run.pop('next_time')

        return last_run


def main():

    # Args is always stronger. Get last run even stronger
    demisto_params = demisto.params() | demisto.args() | demisto.getLastRun()

    demisto_params['params'] = ReqParams.parse_obj(demisto_params)

    request = Request.parse_obj(demisto_params)
    client = Client(request)
    get_events = GetEvents(client)
    command = demisto.command()

    if command == 'test-module':
        get_events.run(max_fetch=1)
        demisto.results('ok')

    else:
        events = get_events.run(int(demisto_params.get('max_fetch', 100)))

        if events:
            get_events.events_to_incidents(events)
            demisto.setLastRun(get_events.set_next_run(events[0]))

        command_results = CommandResults(
            readable_output=tableToMarkdown('Jira records', events, removeNull=True, headerTransform=pascalToSpace),
            outputs_prefix='Jira.Records',
            outputs_key_field='id',
            outputs=events,
            raw_response=events,
        )

        return_results(command_results)


if __name__ in ('__main__', '__builtin__', 'builtins'):
    main()
