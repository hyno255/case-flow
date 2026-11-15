"""Platform settings: agentflow.yaml plus secrets from the environment or a .env file."""
import os
from pathlib import Path

import yaml

from .harness import CLIS

MODEL_DEFAULTS = {"provider": "openai", "name": "", "api_key_env": "OPENAI_API_KEY", "api": "responses", "reasoning": "low"}
MODEL_KEYS = set(MODEL_DEFAULTS) | {"base_url"}
HARNESS_KEYS = {"harness", "name", "api_key_env"}  # a model entry whose work a CLI does: which CLI, which model, and a key if not its own sign-in
DEFAULTS = yaml.safe_load(Path(__file__).with_name("defaults.yaml").read_text())  # every limit and its default; no number lives in the code
SECTIONS = tuple(DEFAULTS)  # limits, engine, show
LIMIT_KEYS = set(DEFAULTS["limits"])
MAY_BE_ZERO = {"cost_per_run_usd"}
GRANT_KEYS = {"secrets"}


def check_numbers(section: str, written, where: str = "") -> list[str]:
    """What is wrong with the values written under one section, in agentflow.yaml or in a flow's `limits`."""
    if not isinstance(written, dict):
        return [f"{where}{section}: must be a mapping of {', '.join(sorted(DEFAULTS[section]))}"]
    errs = [f"{where}{section}: unknown key '{k}'" for k in written if k not in DEFAULTS[section]]
    for k, v in written.items():
        if k in DEFAULTS[section] and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 or (v == 0 and k not in MAY_BE_ZERO)):
            errs.append(f"{where}{section}: {k} must be a number above 0")
    return errs


def model_errors(m, where: str) -> list[str]:
    """What is wrong with one model entry, named in agentflow.yaml or written in a flow."""
    m = m if isinstance(m, dict) else {}
    keys = HARNESS_KEYS if "harness" in m else MODEL_KEYS
    errs = [f"{where}: unknown key '{k}'" + (f"; a model on a CLI takes {', '.join(sorted(HARNESS_KEYS))}" if "harness" in m else "") for k in m if k not in keys]
    if "harness" in m and m["harness"] not in CLIS:
        errs.append(f"{where}: no harness named '{m['harness']}'; the engine knows {', '.join(CLIS)}")
    if not m.get("name"):
        errs.append(f"{where}: missing 'name'")
    return errs


class Settings:
    def __init__(self, home: Path | None = None):
        self.home = Path(home or os.environ.get("AGENTFLOW_HOME") or Path.cwd()).resolve()
        file = self.home / "agentflow.yaml"
        raw = (yaml.safe_load(file.read_text()) or {}) if file.is_file() else {}
        self.models = raw.get("models") or {}   # named models; a flow, a stage or an agent picks one by name
        self.default_model = raw.get("model")   # the name used where nothing picks one
        self.written = {s: raw.get(s) or {} for s in SECTIONS}  # what agentflow.yaml itself sets; the rest are defaults
        self.limits, self.engine, self.show = ({**DEFAULTS[s], **(self.written[s] if isinstance(self.written[s], dict) else {})} for s in SECTIONS)
        self.grants = raw.get("grants") or {}
        self.plugins = self.home / "plugins"
        self.data = self.home / "data"
        self.stand_in = False  # use the scripted model; set by --stand-in and by tests
        self._load_dotenv()

    def _load_dotenv(self) -> None:
        env = self.home / ".env"
        if env.is_file():
            for line in env.read_text().splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

    def limits_for(self, flow_def: dict) -> dict:
        """The limits one flow runs under: the platform's, with the flow's own on top."""
        own = flow_def.get("limits")
        return {**self.limits, **(own if isinstance(own, dict) else {})}

    def proc(self, limits: dict | None = None) -> dict:
        """How a script is run: its time limit, and how the engine watches and stops it. For run_proc."""
        return {"timeout": (limits or self.limits)["script_seconds"], "grace": self.engine["stop_grace_seconds"], "poll": self.engine["poll_seconds"]}

    def granted(self, plugin: str) -> list[str]:
        """The names of the secrets one plugin's scripts were granted, by plugin key such as 'sources/security-tickets'."""
        g = self.grants.get(plugin)
        return [str(x) for x in (g.get("secrets") if isinstance(g, dict) else None) or []]

    def secrets_for(self, plugin: str) -> dict[str, str]:
        """The secrets one script may see, with their values. A script's environment holds nothing else of the engine's."""
        return {name: os.environ[name] for name in self.granted(plugin) if name in os.environ}

    def check(self) -> list[str]:
        """What is wrong in agentflow.yaml itself."""
        errs = [e for s in SECTIONS for e in check_numbers(s, self.written[s])]
        if self.limits["tries"] > self.limits["max_tries"]:
            errs.append("limits: tries cannot be more than max_tries")
        if self.engine["stale_after_seconds"] <= self.engine["heartbeat_seconds"]:
            errs.append("engine: stale_after_seconds must be more than heartbeat_seconds, or a working run looks interrupted")
        for name, m in self.models.items():
            errs += model_errors(m, f"models: {name}")
        if self.default_model not in self.models:
            errs.append("model: name one of the entries under 'models' as the default")
        elif self.harness_of(self.default_model):
            errs.append("model: the default also runs reviewers, which stay in the engine; name a model without 'harness'")
        for key, g in self.grants.items():
            kind, _, name = str(key).partition("/")
            if kind not in ("sources", "tools", "flows") or not (self.plugins / kind / name).is_dir():
                errs.append(f"grants: '{key}' is not a source, tool or flow in the plugins folder")
            if isinstance(g, dict):
                errs += [f"grants: {key}: unknown key '{k}'" for k in g if k not in GRANT_KEYS]
            else:
                errs.append(f"grants: {key}: give 'secrets', a list of variable names")
        return errs

    def model(self, spec=None) -> dict:
        """One model's settings. `spec` is a name from `models:`, a mapping written in a flow, or nothing for the default.
        The key itself is never here: `api_key_env` names the environment variable that holds it.
        An entry with `harness` is returned as written: the CLI it names has its own provider, interface and sign-in."""
        spec = self.default_model if spec is None else spec
        if not isinstance(spec, dict):
            if spec not in self.models:
                raise ValueError(f"agentflow.yaml has no model named '{spec}'; it has {', '.join(self.models) or 'none'}")
            spec = self.models[spec] or {}
        return dict(spec) if "harness" in spec else {**MODEL_DEFAULTS, **spec}

    def harness_of(self, spec=None) -> str | None:
        """The CLI that does the work when this model is picked; None when the engine's own harness does."""
        try:
            return self.model(spec).get("harness")
        except ValueError:
            return None

    def model_label(self, spec=None) -> str:
        try:
            m = self.model(spec)
        except ValueError:
            return "scripted stand-in" if self.stand_in else f"{spec} (not defined)"
        if "harness" in m:
            return f"{m['harness']} CLI · {'scripted stand-in' if self.stand_in else m.get('name', '')}"
        return "scripted stand-in" if self.stand_in else f"{m['provider']} · {m['name']}"

    def check_model(self, spec, where: str) -> list[str]:
        """What is wrong with a model named or written at `where`."""
        if isinstance(spec, dict):
            return model_errors(spec, f"{where}: model")
        if spec not in self.models:
            return [f"{where}: no model named '{spec}' in agentflow.yaml"]
        return []
