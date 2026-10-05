"""Explicit startup wiring, with no environment, migration or network discovery.

Construct once and attach the same application to the HTTP server and worker.
Live readiness is an injected evidence check; constructing ports proves nothing.
Synthetic ports accept JSON fixtures only, never credential/network callbacks.
"""
from copy import deepcopy
from dataclasses import dataclass
import json
import time
from .connectsafely_client import ConnectSafelyClient
from .connectsafely_social_client import ConnectSafelySocialClient
from .connectsafely_transport import ConnectSafelyHTTPS
from .connectsafely_social_transport import ConnectSafelySocialHTTPS
from .connectsafely_console import ConnectSafelyConsole
from .connectsafely_social_console import ConnectSafelySocialConsole
from .connectsafely_application import ConnectSafelyApplication
from .providers.connectsafely import ConnectSafelyAdapter


class _SyntheticResponses:
    def __init__(self,account_id,responses):
        # JSON round-trip excludes executable response callbacks/custom objects.
        self.account_id=account_id
        self.responses=json.loads(json.dumps(responses,allow_nan=False))
        self.calls=[]
    def __call__(self,method,path,query,body):
        self._route(method,path,query,body)
        key=method+' '+path
        if key not in self.responses:raise ValueError('synthetic_response_missing')
        self.calls.append((method,path,deepcopy(query),deepcopy(body)))
        response=self.responses[key]
        if not isinstance(response,list) or len(response)!=2 or type(response[0]) is not int:
            raise ValueError('synthetic_response_invalid')
        return response[0],deepcopy(response[1])


class SyntheticDirectTransport(_SyntheticResponses,ConnectSafelyHTTPS):
    """No sockets, credential callback or HTTP factory exists on this port."""


class SyntheticSocialTransport(_SyntheticResponses,ConnectSafelySocialHTTPS):
    """Synthetic responses still enforce the real one-shot social capability."""


def _assert_simulated(provider):
    if type(provider) is not ConnectSafelyAdapter or type(provider.direct_client) is not ConnectSafelyClient:
        raise ValueError('simulation_requires_typed_synthetic_ports')
    if type(provider.direct_client.request) is not SyntheticDirectTransport:
        raise ValueError('simulation_requires_typed_synthetic_ports')
    social=provider.social_client
    if social is not None and (type(social) is not ConnectSafelySocialClient or type(social.request) is not SyntheticSocialTransport):
        raise ValueError('simulation_requires_typed_synthetic_ports')


@dataclass(frozen=True)
class ConnectSafelyComposition:
    application: ConnectSafelyApplication
    def attach_server(self,server):
        existing=getattr(server,'thunder_connectsafely_app',None)
        if existing is not None and existing is not self.application:raise ValueError('different_application_already_attached')
        server.thunder_connectsafely_app=self.application
        return self.application
    def worker_options(self):
        return {'connectsafely_app':self.application}


def compose_connectsafely(repository,provider,*,direct_ledger=None,social_ledger=None,
                          coordinator=None,author_resolver=None,simulation=False,
                          execution_enabled=False,readiness=None,action_readiness=None,execution_scope=None,clock=time.time):
    if type(simulation) is not bool or type(execution_enabled) is not bool:
        raise ValueError('explicit_boolean_modes_required')
    if type(provider) is not ConnectSafelyAdapter:raise ValueError('canonical_provider_required')
    if simulation:_assert_simulated(provider)
    account=dict(account_id=provider.account_id,account_public_id=provider.expected_public_id,
                 writes_enabled=execution_enabled,clock=clock)
    direct=ConnectSafelyConsole(repository,provider,direct_ledger,**account) if direct_ledger is not None else None
    social=(ConnectSafelySocialConsole(repository,provider,social_ledger,coordinator=coordinator,
            author_resolver=author_resolver,**account)
            if social_ledger is not None and provider.social_client is not None else None)
    app=ConnectSafelyApplication(repository,direct_console=direct,social_console=social,provider=provider,
        simulation=simulation,execution_enabled=execution_enabled,readiness=readiness,action_readiness=action_readiness,execution_scope=execution_scope,clock=clock)
    return ConnectSafelyComposition(app)
