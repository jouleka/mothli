"""Release-blocking owner-scope and operation-inventory gates for LIT-48."""

from __future__ import annotations

import ast
import importlib
import inspect
import json
import pkgutil
import sys
import uuid
from pathlib import Path

import pytest

from app.hosted.tenant.models import OwnerId
from app.hosted.tenant.repository import PostgresTenantRepository

INVENTORY = Path(__file__).parents[2] / "app" / "hosted" / "tenant" / "endpoints.json"


def test_every_public_tenant_repository_method_requires_owner_id() -> None:
    infrastructure_methods = {"check_runtime_role"}
    methods = {
        name: member
        for name, member in inspect.getmembers(PostgresTenantRepository, inspect.isfunction)
        if not name.startswith("_") and name not in infrastructure_methods
    }
    assert methods
    for name, method in methods.items():
        parameters = inspect.signature(method).parameters
        assert "owner_id" in parameters, f"{name} is missing the explicit owner boundary"


_OWNER_LIKE = {"owner", "ownerid", "owners", "userid", "user", "tenant", "tenantid", "principal",
               "accountid", "account", "session", "sessionid"}


def _owner_like(parameter: inspect.Parameter) -> bool:
    name = parameter.name.casefold().replace("_", "").replace("-", "")
    return name in _OWNER_LIKE or parameter.annotation in (OwnerId, "OwnerId")


def _owner_boundary_breaks(cls: type) -> list[str]:
    """Why ``cls`` is not owner-bound the way ADR 0041 section 1 requires (empty when it is)."""
    breaks = []
    init = inspect.signature(cls.__init__).parameters
    owner = init.get("owner_id")
    if owner is None or owner.default is not inspect.Parameter.empty:
        breaks.append(f"{cls.__name__} does not require owner_id in its constructor")
    elif owner.annotation not in (OwnerId, "OwnerId"):
        breaks.append(f"{cls.__name__}.__init__ owner_id is not typed OwnerId")
    for name, member in inspect.getmembers(cls):
        if name == "__init__":
            continue
        function = member.fget if isinstance(member, property) else member
        if isinstance(member, property) and member.fset is not None:
            breaks.append(f"{cls.__name__}.{name} can be assigned")
        if not (inspect.isfunction(function) or inspect.ismethod(function)):
            continue
        if not function.__module__.startswith("app.hosted.companion"):
            continue
        for parameter in list(inspect.signature(function).parameters.values()):
            if _owner_like(parameter):
                breaks.append(f"{cls.__name__}.{name} accepts an owner-like argument")
    return breaks


def test_every_companion_adapter_is_bound_to_one_owner_at_construction() -> None:
    """The break it catches: an adapter method that takes an owner (so a caller could pass another
    reader's), an adapter built without one, or an owner that can be swapped after construction.

    ADR 0021 asks every repository method for ``owner_id``; the companion modules call the local
    catalog's names, so ADR 0041 moves the owner into the constructor instead and checks it here.
    """
    import app.hosted.companion as companion
    from app.hosted.companion import OwnerBoundAdapter

    for info in pkgutil.iter_modules(companion.__path__):
        importlib.import_module(f"app.hosted.companion.{info.name}")
    adapters = {
        cls
        for module in list(sys.modules.values())
        if (getattr(module, "__name__", "") or "").startswith("app.hosted.companion")
        for _name, cls in inspect.getmembers(module, inspect.isclass)
        if issubclass(cls, OwnerBoundAdapter)
    }
    from app.hosted.companion.store import HostedCompanionCatalog

    assert HostedCompanionCatalog in adapters
    for cls in adapters:
        assert _owner_boundary_breaks(cls) == [], cls

    owner = OwnerId(uuid.uuid4())
    catalog = HostedCompanionCatalog("postgresql://unused.invalid/db", owner)
    with pytest.raises(AttributeError):
        catalog._owner = OwnerId(uuid.uuid4())
    with pytest.raises(AttributeError):
        del catalog._owner
    with pytest.raises(TypeError):
        HostedCompanionCatalog("postgresql://unused.invalid/db", owner.value)
    assert str(owner.value) not in repr(catalog)

    # The check itself must catch the break it exists for: a method taking an owner.
    class Leaky(HostedCompanionCatalog):
        __slots__ = ()

        def get_read_map(self, book_id, *, incarnation, owner_id=None):
            return {}

    Leaky.__module__ = "app.hosted.companion.leaky"
    Leaky.get_read_map.__module__ = "app.hosted.companion.leaky"
    assert _owner_boundary_breaks(Leaky) == ["Leaky.get_read_map accepts an owner-like argument"]


def test_endpoint_inventory_names_cross_tenant_evidence_and_unavailable_surfaces() -> None:
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    assert inventory["version"] == 19
    assert all(item["cross_tenant_test"] for item in inventory["enabled"])
    assert all(item["cross_tenant_test"] for item in inventory["unavailable"])
    assert all(item["cross_tenant_test"] for item in inventory["background"])
    evidence_files = Path(__file__).parent.glob("test_*.py")
    evidence_names = {
        node.name
        for evidence_file in evidence_files
        for node in ast.walk(ast.parse(evidence_file.read_text(encoding="utf-8")))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    named_evidence = {
        item["cross_tenant_test"]
        for group in ("enabled", "unavailable", "background")
        for item in inventory[group]
    }
    assert named_evidence <= evidence_names
    surfaces = {
        item["surface"] for group in ("enabled", "unavailable") for item in inventory[group]
    }
    assert surfaces == set(inventory["required_surface_classes"])
    assert {item["resource"] for item in inventory["background"]} == {
        "worker-claim-and-mutations",
        "worker-credential-resolution",
        "filesystem-source-storage",
        "s3-source-storage",
        "runtime-cache-and-locks",
        "companion-jobs",
        "account-purge",
    }
    unavailable = {item["access"] for item in inventory["unavailable"]}
    assert unavailable == {"export"}


def test_inventory_marks_exactly_the_routes_hidden_without_reader_keys() -> None:
    """The break it catches: a reader-key route mounted while HOSTED_READER_KEYS is false but not
    marked in the inventory (or marked but still mounted), so the inventory would no longer say
    what a signed-in reader can reach. Each marked route keeps its cross-tenant test, which runs
    with the setting on (ADR 0041 section 6)."""
    from app.hosted.tenant.api import READER_KEY_ROUTES, hosted_router, router

    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    marked = {(item["method"], item["path"]) for item in inventory["enabled"]
              if item.get("mounted_only_with") == "HOSTED_READER_KEYS"}
    assert marked == READER_KEY_ROUTES
    assert all(item.get("mounted_only_with") in (None, "HOSTED_READER_KEYS")
               for item in inventory["enabled"])

    def mounted(chosen):
        return {(method, route.path) for route in chosen.routes
                for method in getattr(route, "methods", ()) or ()}

    everything = mounted(router)
    assert READER_KEY_ROUTES <= everything
    assert mounted(hosted_router(reader_keys=True)) == everything
    assert mounted(hosted_router(reader_keys=False)) == everything - READER_KEY_ROUTES
