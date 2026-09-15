"""Provider selection and automatic fallback chain."""

from __future__ import annotations

from ..config import Settings
from .base import BikeType, RouteRequest, RouteResult, RoutingError, RoutingProvider
from .brouter import BRouterProvider
from .graphhopper import GraphHopperProvider
from .openrouteservice import OpenRouteServiceProvider


def priority_for(bike_type: BikeType) -> list[str]:
    """BRouter first for off-road styles, ORS first for road/gravel."""
    if bike_type in (BikeType.mtb, BikeType.trekking):
        return ["brouter", "openrouteservice", "graphhopper"]
    return ["openrouteservice", "brouter", "graphhopper"]


def build_provider(name: str, settings: Settings) -> RoutingProvider | None:
    if name == "brouter":
        return BRouterProvider(settings.brouter_url, settings.http_timeout_s)
    if name == "openrouteservice":
        if not settings.has_ors:
            return None
        assert settings.ors_api_key is not None
        return OpenRouteServiceProvider(
            settings.ors_api_key, settings.ors_base_url, settings.http_timeout_s
        )
    if name == "graphhopper":
        if not settings.has_graphhopper:
            return None
        assert settings.graphhopper_api_key is not None
        return GraphHopperProvider(
            settings.graphhopper_api_key, settings.graphhopper_base_url, settings.http_timeout_s
        )
    return None


def providers_for(bike_type: BikeType, settings: Settings) -> list[RoutingProvider]:
    providers: list[RoutingProvider] = []
    for name in priority_for(bike_type):
        provider = build_provider(name, settings)
        if provider is not None:
            providers.append(provider)
    return providers


async def route_with_fallback(
    providers: list[RoutingProvider], req: RouteRequest
) -> tuple[RouteResult, list[str]]:
    """Try each provider in order; return the first success plus fallback notes."""
    warnings: list[str] = []
    for provider in providers:
        try:
            result = await provider.route(req)
            return result, warnings
        except RoutingError as exc:
            warnings.append(f"Routing provider '{provider.name}' fehlgeschlagen: {exc}")
        except Exception as exc:  # noqa: BLE001 - keep the chain alive
            warnings.append(f"Routing provider '{provider.name}' Fehler: {exc}")
    raise RoutingError(
        "Kein Routing-Provider erfolgreich. " + " | ".join(warnings)
    )
