"""Import-safety tests for the Discord gateway adapter."""

import builtins
import importlib
import sys


class TestDiscordImportSafety:
    def test_module_imports_even_when_discord_dependency_is_missing(self, monkeypatch):
        original_import = builtins.__import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "discord" or name.startswith("discord."):
                raise ImportError("discord unavailable for test")
            return original_import(name, globals, locals, fromlist, level)

        # Purge the cached module so the import below actually re-runs the
        # module body with discord.py simulated-missing.
        monkeypatch.delitem(sys.modules, "plugins.platforms.discord.adapter", raising=False)
        monkeypatch.delitem(sys.modules, "plugins.platforms.discord", raising=False)
        # Re-importing the purged submodule also re-sets the ``discord``
        # attribute on the parent ``plugins.platforms`` package (pointing at
        # the fresh, discord-less module). monkeypatch.delitem only restores
        # sys.modules, not that attribute — pin it here so teardown restores
        # the original and later ``import plugins.platforms.discord.adapter
        # as ...`` statements (attribute-chain resolution) see the real
        # module with ``discord`` bound.
        import plugins.platforms as _platforms_pkg

        if hasattr(_platforms_pkg, "discord"):
            monkeypatch.setattr(
                _platforms_pkg, "discord", getattr(_platforms_pkg, "discord")
            )
        monkeypatch.setattr(builtins, "__import__", fake_import)

        module = importlib.import_module("plugins.platforms.discord.adapter")

        assert module.DISCORD_AVAILABLE is False
        assert module.discord is None
