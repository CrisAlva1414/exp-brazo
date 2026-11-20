"""Carga, guardado versionado y rollback de la configuración de la aplicación."""

from __future__ import annotations

import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any

import yaml

from .contracts import AppConfig


class ConfigError(Exception):
    """Error controlado de carga o versionado de configuración."""


_VERSION_RE = re.compile(r"^v(\d+)\.yaml$")


def _atomic_write_yaml(path: Path, payload: Any) -> None:
    """Escribe YAML de forma atómica en el mismo directorio de destino."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(payload, handle, sort_keys=False, allow_unicode=True)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


class ConfigStore:
    """Mantiene la configuración validada y su historial append-only."""

    def __init__(
        self,
        path: Path = Path("config/config.yaml"),
        versions_dir: Path = Path("config_versions"),
    ) -> None:
        self.path = Path(path)
        self.versions_dir = Path(versions_dir)
        self._lock = threading.RLock()
        self._config: AppConfig | None = None
        self.load()

    def load(self) -> AppConfig:
        """Lee y valida ``config.yaml``, actualizando la copia en memoria."""

        with self._lock:
            if not self.path.exists():
                raise ConfigError(
                    "No existe config/config.yaml. Ejecutá ./install.sh o ./start.sh --check."
                )

            try:
                raw = yaml.safe_load(self.path.read_text(encoding="utf-8"))
            except OSError as exc:
                raise ConfigError(f"No se pudo leer {self.path}: {exc}") from exc
            except yaml.YAMLError as exc:
                raise ConfigError(f"Configuración YAML inválida en {self.path}: {exc}") from exc

            if raw is None:
                raise ConfigError(f"La configuración {self.path} está vacía.")

            config = AppConfig.model_validate(raw)
            self._config = config
            self._archive_loaded(config)
            return config.model_copy(deep=True)

    def get(self) -> AppConfig:
        """Devuelve una copia profunda de la configuración vigente."""

        with self._lock:
            if self._config is None:
                self.load()
            assert self._config is not None
            return self._config.model_copy(deep=True)

    def save(self, new: AppConfig | dict[str, Any], reason: str = "") -> AppConfig:
        """Valida, versiona y persiste una nueva configuración."""

        del reason  # reservado para auditoría futura; no altera el contrato
        with self._lock:
            current = self._require_config()
            if isinstance(new, AppConfig):
                payload = new.model_dump(mode="python")
            else:
                payload = dict(new)

            payload["version"] = current.version + 1
            validated = AppConfig.model_validate(payload)

            _atomic_write_yaml(self.path, validated.model_dump(mode="json"))
            self._write_version_file(validated)
            self._config = validated
            return validated.model_copy(deep=True)

    def rollback(self) -> AppConfig:
        """Restaura la versión inmediatamente anterior del historial."""

        with self._lock:
            current = self._require_config()
            candidates: list[tuple[int, Path]] = []
            for path in self.version_files():
                match = _VERSION_RE.match(path.name)
                if match is None:
                    continue
                number = int(match.group(1))
                if number < current.version:
                    candidates.append((number, path))

            if not candidates:
                raise ConfigError("No hay versión anterior para restaurar.")

            _, previous_path = max(candidates, key=lambda item: item[0])
            try:
                raw = yaml.safe_load(previous_path.read_text(encoding="utf-8"))
            except OSError as exc:
                raise ConfigError(f"No se pudo leer {previous_path}: {exc}") from exc
            except yaml.YAMLError as exc:
                raise ConfigError(f"Versión YAML inválida en {previous_path}: {exc}") from exc

            previous = AppConfig.model_validate(raw)
            payload = previous.model_dump(mode="python")
            payload["version"] = current.version + 1
            restored = AppConfig.model_validate(payload)

            _atomic_write_yaml(self.path, restored.model_dump(mode="json"))
            self._write_version_file(restored)
            self._config = restored
            return restored.model_copy(deep=True)

    def version_files(self) -> list[Path]:
        """Lista las versiones guardadas, ordenadas por número ascendente."""

        with self._lock:
            if not self.versions_dir.exists():
                return []
            paths = [p for p in self.versions_dir.glob("v*.yaml") if _VERSION_RE.match(p.name)]
            return sorted(paths, key=lambda p: int(_VERSION_RE.match(p.name).group(1)))  # type: ignore[union-attr]

    def _require_config(self) -> AppConfig:
        if self._config is None:
            self.load()
        assert self._config is not None
        return self._config

    def _write_version_file(self, config: AppConfig) -> None:
        target = self.versions_dir / f"v{config.version:04d}.yaml"
        _atomic_write_yaml(target, config.model_dump(mode="json"))

    def _archive_loaded(self, config: AppConfig) -> None:
        """Garantiza el snapshot inicial para que el rollback tenga historial."""

        target = self.versions_dir / f"v{config.version:04d}.yaml"
        if target.exists():
            return
        try:
            self._write_version_file(config)
        except OSError:
            # La carga de configuración no debe fallar por un directorio de
            # versiones no escribible; el guardado sí reportará el error.
            pass
