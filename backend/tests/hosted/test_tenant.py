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
    assert inventory["version"] == 13
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
    }
    unavailable = {item["access"] for item in inventory["unavailable"]}
    assert unavailable == {"export"}
