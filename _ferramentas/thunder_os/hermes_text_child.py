"""One authorized text-only inference child using the existing Hermes runtime.

Operation mode uses guarded read-only OAuth and config projection. The legacy
authorized single-trial mode may refresh OAuth and create Hermes config backups.
No tokens are printed, copied to disk or passed through argv/stdout.
"""
from __future__ import annotations
import argparse
from contextlib import redirect_stdout
from datetime import date
import json
import logging
import os
from pathlib import Path
import sys

from .agents import AgentJob, AgentRunnerError
from .hermes_text_adapter import HermesTextAdapter, LIMIT, load_catalog_context


def install_hermes_origin_guard(root):
    """Pin all Hermes modules to the selected tree, including editable-finder names."""
    from importlib.machinery import PathFinder
    root = Path(root).resolve(strict=True)
    names = {p.stem for p in root.glob("*.py")} | {p.name for p in root.iterdir() if p.is_dir()}
    names |= {"agent", "hermes_cli", "providers", "utils", "hermes_constants"}
    for module_name, module in tuple(sys.modules.items()):
        if module_name.startswith("__editable__"):
            for key in ("MAPPING", "NAMESPACES"):
                mapping = getattr(module, key, {})
                if isinstance(mapping, dict):
                    names.update(name.split(".")[0] for name in mapping)
    def validate():
        for name, module in tuple(sys.modules.items()):
            if name.split(".")[0] not in names:
                continue
            paths = list(getattr(module, "__path__", ()))
            filename = getattr(module, "__file__", None)
            if filename:
                paths.append(filename)
            origin = getattr(getattr(module, "__spec__", None), "origin", None)
            if origin:
                paths.append(origin)
            if not paths or any(not Path(path).resolve().is_relative_to(root) for path in paths):
                raise ImportError("Hermes dependency origin mismatch")
    class PinnedFinder:
        def find_spec(self, fullname, path=None, target=None):
            if fullname.split(".")[0] not in names:
                return None
            search = [str(root)] if path is None else list(path)
            if any(not Path(value).resolve().is_relative_to(root) for value in search):
                raise ImportError("Hermes dependency origin mismatch")
            spec = PathFinder.find_spec(fullname, search, target)
            if spec is None:
                raise ModuleNotFoundError("Hermes module missing from isolated dependency")
            locations = list(spec.submodule_search_locations or ())
            if spec.origin:
                locations.append(spec.origin)
            if not locations or any(not Path(value).resolve().is_relative_to(root) for value in locations):
                raise ImportError("Hermes dependency origin mismatch")
            return spec
    validate()
    sys.meta_path.insert(0, PinnedFinder())
    return validate


def configured_adapter(*, brain: Path, evidence: Path, hermes_root: Path, profile: Path, mode: str = "trial"):
    if mode not in {"trial", "operation"}:
        raise ValueError("explicit text mode required")
    if any(os.environ.get(key, "false") != "false" for key in ("THUNDER_EXTERNAL_EFFECTS_ENABLED", "THUNDER_OS_EXTERNAL_EFFECTS_ENABLED")):
        raise ValueError("external effects must remain disabled")
    if not (hermes_root / "hermes_cli/runtime_provider.py").is_file() or not (profile / "config.yaml").is_file():
        raise ValueError("existing Hermes installation and profile required")
    os.environ["HERMES_HOME"] = str(profile)
    # Fixed operator paths, never sourced from the work objective/context.
    sys.path.insert(0, str(hermes_root))
    logging.disable(logging.CRITICAL)
    if mode == "operation":
        # Fixed policy, set before any Hermes import; only isolated patched dependency supports it.
        os.environ["HERMES_AUTH_READ_ONLY"] = "1"
        from .hermes_auth_ro import load_operation_config, resolve_operation_runtime, create_operation_client, readonly_resolution
        from .hermes_dependency import verify_dependency
        with readonly_resolution():
            verify_dependency(hermes_root)
            validate_origins = install_hermes_origin_guard(hermes_root)
            from agent.model_metadata import strip_codex_context_variant_suffix
            validate_origins()
        def _wire_model_identity(model):
            return str(strip_codex_context_variant_suffix(model or "")).strip() or None
        config_loader = lambda: load_operation_config(profile)
        def runtime_loader(**kwargs):
            with readonly_resolution():
                result = resolve_operation_runtime(**kwargs)
                validate_origins()
                return result
        create_client = create_operation_client
    else:
        from agent.codex_responses_adapter import _wire_model_identity
        from hermes_cli.config import load_config_readonly
        from hermes_cli.runtime_provider import resolve_runtime_provider
        from agent.auxiliary_client import _create_openai_client
        config_loader, runtime_loader, create_client = load_config_readonly, resolve_runtime_provider, _create_openai_client

    def context_loader(job):
        context = load_catalog_context(brain)
        with evidence.open("rb") as stream:
            raw = stream.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError("evidence bundle too large")
        bundle = json.loads(raw)
        if not isinstance(bundle, dict) or bundle.get("account_id") != job.payload.get("account_id"):
            raise ValueError("trusted evidence account mismatch")
        context["current_date"] = date.today().isoformat()
        context["research_evidence"] = bundle.get("research_evidence", [])
        if "source_coverage" in bundle:
            context["source_coverage"] = bundle["source_coverage"]
        if "research_account" in bundle:
            context["research_account"] = bundle["research_account"]
        if "research_producer" in bundle:
            context["research_producer"] = bundle["research_producer"]
        if mode == "trial":
            context["trial_constraint"] = "Research-only trial: recommendation must be pesquisar mais; do not prospect or send."
        else:
            context["operation_constraint"] = "Internal research or draft only. Never authorize or execute any external effect."
        context["style_rules"] = bundle.get("style_rules", {})
        context["angle_ids"] = bundle.get("angle_ids", [])
        return context

    def client_factory(**kwargs):
        if mode == "operation":
            with readonly_resolution():
                client = create_client(**kwargs)
                validate_origins()
        else:
            client = create_client(**kwargs)
        create = client.responses.create
        # Same wire-model normalization as Hermes, without choosing a new model.
        def normalized_create(**request):
            request["model"] = _wire_model_identity(request["model"])
            return create(**request)
        client.responses.create = normalized_create
        return client
    return HermesTextAdapter(config_loader=config_loader, runtime_loader=runtime_loader,
        client_factory=client_factory, context_loader=context_loader)


def diagnostic_code(exc, stage):
    from .hermes_auth_ro import ReadOnlyAuthError
    if isinstance(exc, ReadOnlyAuthError):
        return exc.code
    status = getattr(exc, "status_code", None)
    if status in (401, 403, 429):
        return {401: "auth_unauthorized", 403: "auth_forbidden", 429: "rate_limited"}[status]
    if isinstance(exc, TimeoutError):
        return "request_timeout"
    if isinstance(exc, PermissionError):
        return "tool_output_blocked"
    if isinstance(exc, json.JSONDecodeError) or stage == "schema":
        return "invalid_output_schema"
    if isinstance(exc, AgentRunnerError):
        return "text_contract_rejected"
    return "bootstrap_failed" if stage == "bootstrap" else "inference_failed"


def main(argv=None):
    parser = argparse.ArgumentParser()
    for field in ("brain", "evidence", "hermes-root", "profile", "diagnostic"):
        parser.add_argument("--" + field, required=True, type=Path)
    parser.add_argument("--mode", choices=("trial", "operation"), default="trial")
    args = parser.parse_args(argv)
    stage = "bootstrap"
    try:
        raw = sys.stdin.buffer.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError("input too large")
        data = json.loads(raw)
        if set(data) != {"id", "type", "payload"}:
            raise ValueError("invalid job")
        with open(os.devnull, "w") as quiet, redirect_stdout(quiet):
            adapter = configured_adapter(brain=args.brain.resolve(strict=True), evidence=args.evidence.resolve(strict=True),
                hermes_root=args.hermes_root.resolve(strict=True), profile=args.profile.resolve(strict=True), mode=args.mode)
            # Parent enforces the hard 120-second process-group deadline including
            # config/auth work. The HTTP request also has this timeout, no retries.
            stage = "inference"
            result = adapter(AgentJob(**data), 120)
        stage = "schema"
        output = json.loads(result)
        if args.mode == "trial" and data["type"] == "research" and output.get("artifact", {}).get("recommendation") != "pesquisar mais":
            raise ValueError("trial must remain research-only")
        from .agent_consumer import atomic_json
        atomic_json(args.diagnostic, {"status": "completed", "model": getattr(adapter, "model_name", None),
            "external_effects_enabled": False})
        sys.stdout.write(result)
        return 0
    except Exception as exc:
        from .agent_consumer import atomic_json
        atomic_json(args.diagnostic, {"status": "failed", "error_code": diagnostic_code(exc, stage),
            "stage": stage, "external_effects_enabled": False})
        # Neither configuration values nor auth/provider error text leave child.
        sys.stderr.write("hermes_text_trial_failed\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
