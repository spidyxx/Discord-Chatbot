"""Plugin registry — discovery and dispatch."""

import configparser
import importlib
import logging
import pkgutil
from pathlib import Path
from typing import TYPE_CHECKING

from .base import MessageContext, Plugin

_log = logging.getLogger(__name__)

_VALID_TIERS = {"cheap", "normal", "expensive"}


def _apply_cfg(cfg_path: Path, plugins: list[Plugin]) -> None:
    """Read a plugin .cfg file and apply [plugin] model_tier to the given plugins."""
    if not cfg_path.exists() or not plugins:
        return
    cfg = configparser.ConfigParser()
    cfg.read(cfg_path)
    tier = cfg.get("plugin", "model_tier", fallback=None)
    if tier is None:
        return
    if tier not in _VALID_TIERS:
        _log.warning(f"Invalid model_tier {tier!r} in {cfg_path.name}, ignoring")
        return
    for plugin in plugins:
        plugin.model_tier = tier
        _log.info(f"{plugin.__class__.__name__}: model_tier={tier!r} (from {cfg_path.name})")


class Registry:
    def __init__(self):
        self._plugins:    list[Plugin]        = []
        self._intent_map: dict[str, Plugin]   = {}

    def register(self, plugin: Plugin) -> None:
        for intent in plugin.INTENTS:
            if intent in self._intent_map:
                _log.warning(
                    f"Intent {intent!r} already claimed by "
                    f"{self._intent_map[intent].__class__.__name__}, overwriting"
                )
            self._intent_map[intent] = plugin
        self._plugins.append(plugin)
        _log.info(f"Plugin registered: {plugin.__class__.__name__} → {plugin.INTENTS}")

    def handles(self, intent: str) -> bool:
        return intent in self._intent_map

    def intent_lines(self) -> list[str]:
        """Classifier prompt lines from all plugins, sorted by intent_order."""
        ordered = sorted(self._plugins, key=lambda p: p.intent_order)
        lines = []
        for plugin in ordered:
            lines.extend(plugin.INTENT_LINES)
        return lines

    def intent_prefixes(self) -> list[tuple[str, str]]:
        """(prefix, label) pairs for the classify_intent matching loop."""
        result = []
        for plugin in sorted(self._plugins, key=lambda p: p.intent_order):
            for label in plugin.INTENTS:
                prefix = plugin.INTENT_PREFIXES.get(label, label)
                result.append((prefix, label))
        return result

    def pre_classify(self, clean: str) -> tuple[str, str] | None:
        """Try deterministic pre-classification across all plugins (before Haiku)."""
        for plugin in self._plugins:
            result = plugin.pre_classify(clean)
            if result is not None:
                return result
        return None

    async def on_ready(self) -> None:
        """Call on_ready() on every plugin that overrides it."""
        for plugin in self._plugins:
            await plugin.on_ready()

    async def dispatch(self, ctx: MessageContext) -> bool:
        """Call the matching plugin. Returns True if handled."""
        plugin = self._intent_map.get(ctx.intent)
        if plugin is None:
            return False
        await plugin.handle(ctx)
        return True

    def model_tier_for(self, intent: str) -> str | None:
        """Return model_tier of the plugin handling intent, or None if not set."""
        plugin = self._intent_map.get(intent)
        return plugin.model_tier if plugin is not None else None

    def __repr__(self) -> str:
        return (
            f"<Registry plugins={[p.__class__.__name__ for p in self._plugins]} "
            f"intents={list(self._intent_map)}>"
        )


# Module-level singleton — bot.py and plugins both import this instance.
registry = Registry()


def discover() -> Registry:
    """Import all plugin packages and call their setup(registry) functions."""
    for pkg_name in ["plugins.core", "plugins.community"]:
        try:
            pkg = importlib.import_module(pkg_name)
            for _, module_name, _ in pkgutil.iter_modules(pkg.__path__):
                full_name = f"{pkg_name}.{module_name}"
                try:
                    mod = importlib.import_module(full_name)
                    if hasattr(mod, "setup") and callable(mod.setup):
                        _before = set(id(p) for p in registry._plugins)
                        mod.setup(registry)
                        _new = [p for p in registry._plugins if id(p) not in _before]
                        _apply_cfg(Path(mod.__file__).with_suffix(".cfg"), _new)
                        _log.info(f"Discovered plugin module: {full_name}")
                    else:
                        _log.warning(f"Plugin module {full_name} has no setup() function")
                except Exception:
                    _log.exception(f"Failed to load plugin module: {full_name}")
        except ImportError:
            pass  # plugins/community/ may not exist yet
    return registry
