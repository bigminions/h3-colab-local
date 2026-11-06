"""Application and compute contracts used by the local workbench.

Registries describe installed adapters, never provision resources. A provider is
created only for an executing controller; switching a page is a local operation.
"""
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Protocol


class ComputeProvider(Protocol):
    def connect(self, config: Any) -> Any: ...
    def execute(self, job: dict) -> Any: ...
    def download(self, result: dict, target: Path) -> Any: ...
    def release(self) -> Any: ...
    def consumer_status(self) -> dict: ...
    def cancel_job(self, job: dict) -> Any: ...


@dataclass(frozen=True)
class ApplicationSpec:
    id: str
    name: str
    description: str
    output_kind: str
    providers: tuple[str, ...]
    tabs: tuple[dict, ...]
    asset_purposes: tuple[dict, ...] = ()


class ApplicationProtocol(Protocol):
    spec: ApplicationSpec
    config_model: type

    def source(self, base: Path, config: dict) -> str: ...
    def invocation(self, source: str, job: dict) -> str: ...
    def input_assets(self, config: dict) -> list[str]: ...


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    name: str
    available: bool
    description: str
    gpus: tuple[str, ...] = ()


class ApplicationRegistry:
    def __init__(self, applications=()):
        self._applications = {app.spec.id: app for app in applications}

    def register(self, application):
        self._applications[application.spec.id] = application

    def get(self, app_id):
        try:
            return self._applications[app_id]
        except KeyError:
            raise ValueError('应用未安装：' + app_id) from None

    def list(self):
        return [asdict(app.spec) for app in self._applications.values()]

    def __iter__(self):
        return iter(self._applications.values())

    def __contains__(self, app_id):
        return app_id in self._applications


class ProviderRegistry:
    def __init__(self, specs):
        self._specs = {spec.id: spec for spec in specs}
        self._factories = {}

    def register(self, provider_id, factory):
        spec = self.get(provider_id)
        if not spec.available:
            raise ValueError('算力提供商尚未接入：' + provider_id)
        self._factories[provider_id] = factory

    def get(self, provider_id):
        try:
            return self._specs[provider_id]
        except KeyError:
            raise ValueError('未知算力提供商：' + provider_id) from None

    def validate(self, app, provider_id, gpu):
        spec = self.get(provider_id)
        if not spec.available or provider_id not in self._factories:
            raise ValueError(spec.name + ' 尚未接入')
        if provider_id not in app.spec.providers:
            raise ValueError('该应用不支持所选算力提供商')
        if spec.gpus and gpu not in spec.gpus and provider_id != 'colab':
            raise ValueError('所选算力不支持该显卡')

    def create(self, provider_id, controller):
        spec = self.get(provider_id)
        if not spec.available or provider_id not in self._factories:
            raise ValueError(spec.name + ' 尚未接入')
        return self._factories[provider_id](controller)

    def list(self):
        return [asdict(spec) for spec in self._specs.values()]
