"""Signed join invites and one-line join codes.

An invite is a JSON document signed by the coordinator identity. It carries
everything a worker needs (endpoint, topic, context, pubkey, run fingerprint)
plus a one-time join token. The token is enforced server-side with expiry and
a maximum number of uses, and the invite signature prevents tampering with
the endpoint/topic while it is being shared as a link or QR code.

Public API:
    verify_invite(invite)          -> bool
    make_join_code(invite)         -> "didiloco://join?d=..."
    parse_join_code(value)         -> invite dict (accepts code, JSON or path)
    friendly_error(message)        -> human-readable explanation
"""
from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Union

from .identity import canonical_invite, verify_message

JOIN_SCHEME = "didiloco://join?"
INVITE_TYPE = "diloco-invite-v1"


class InviteError(Exception):
    pass


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def verify_invite(invite: Mapping[str, Any], *, now: Optional[float] = None, require_sig: bool = True) -> bool:
    """Verify signature, expiry and basic shape of an invite."""
    if str(invite.get("type", "")) != INVITE_TYPE:
        return False
    expires = invite.get("expires_at")
    if expires is not None:
        try:
            if float(expires) <= (time.time() if now is None else now):
                return False
        except (TypeError, ValueError):
            return False
    sig = str(invite.get("sig", "") or "")
    signed_by = str(invite.get("signed_by", "") or "")
    if require_sig:
        if not sig or not signed_by:
            return False
        unsigned = {k: v for k, v in invite.items() if k not in ("sig", "sig_alg", "signed_by")}
        try:
            return verify_message(signed_by, canonical_invite(unsigned), sig)
        except Exception:
            return False
    return True


def make_join_code(invite: Mapping[str, Any]) -> str:
    """Short, QR-friendly code: endpoint + token + run + key hint.

    The full signed invite (including config) is fetched from the coordinator
    with the token, so the code stays around ~120 characters instead of KB.
    """
    params = {
        "h": str(invite.get("endpoint", "") or ""),
        "t": str(invite.get("join_token", "") or ""),
        "r": str(invite.get("run_id", "") or ""),
        "k": str(invite.get("signed_by", "") or "")[:16],
    }
    return JOIN_SCHEME + urllib.parse.urlencode(params)


def _join_code_params(text: str) -> Dict[str, str]:
    parsed = urllib.parse.urlparse(text)
    values = urllib.parse.parse_qs(parsed.query)
    params = {k: (v[-1] if v else "") for k, v in values.items()}
    if not params.get("h") or not params.get("t"):
        raise InviteError("código de unión incompleto (falta endpoint o token)")
    return params


def fetch_invite(endpoint: str, token: str, timeout: float = 15.0) -> Dict[str, Any]:
    url = str(endpoint).rstrip("/") + "/v1/invite?" + urllib.parse.urlencode({"token": token})
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            obj = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise InviteError(f"el coordinador rechazó el token de unión (HTTP {exc.code})") from exc
    except Exception as exc:  # noqa: BLE001
        raise InviteError(f"no se pudo descargar la invitación del coordinador: {exc}") from exc
    if not isinstance(obj, dict):
        raise InviteError("invitación descargada no válida")
    return obj


def parse_join_code(value: Union[str, Path, Mapping[str, Any]], *, verify: bool = True) -> Dict[str, Any]:
    """Accept a join code, a JSON string, a path to a JSON file or a dict."""
    if isinstance(value, Mapping):
        invite = dict(value)
    else:
        text = str(value).strip()
        if text.startswith("didiloco:"):
            params = _join_code_params(text)
            invite = fetch_invite(params["h"], params["t"])
            if str(invite.get("join_token", "")) != params["t"]:
                raise InviteError("el token de la invitación no coincide con el código")
            if params.get("r") and str(invite.get("run_id", "")) != params["r"]:
                raise InviteError("la invitación es de otra sesión (run_id distinto)")
            if params.get("k") and not str(invite.get("signed_by", "")).startswith(params["k"]):
                raise InviteError("la invitación no fue firmada por la clave esperada")
        elif text.startswith("{"):
            try:
                invite = json.loads(text)
            except json.JSONDecodeError as exc:
                raise InviteError("JSON de invitación no válido") from exc
        else:
            path = Path(text).expanduser()
            if not path.is_file():
                raise InviteError(f"no se encontró la invitación: {text}")
            try:
                content = path.read_text(encoding="utf-8").strip()
            except Exception as exc:  # noqa: BLE001
                raise InviteError(f"no se pudo leer la invitación: {exc}") from exc
            if content.startswith("didiloco:"):
                # A code stored in a file (e.g. by the dashboard agent) keeps
                # the token out of process command lines and logs.
                import os

                try:
                    os.chmod(path, 0o600)
                except Exception:
                    pass
                invite = parse_join_code(content, verify=False)
            else:
                try:
                    invite = json.loads(content)
                except Exception as exc:  # noqa: BLE001
                    raise InviteError(f"no se pudo leer la invitación: {exc}") from exc
    if not isinstance(invite, dict):
        raise InviteError("la invitación debe ser un objeto JSON")
    if verify and not verify_invite(invite):
        raise InviteError(
            "invitación no válida: firma incorrecta o caducada. "
            "Pide una invitación nueva al coordinador."
        )
    return invite


def friendly_error(message: str) -> str:
    """Translate common coordinator errors into plain Spanish (Nivel 3)."""
    text = str(message or "")
    lower = text.lower()
    # Never echo bearer tokens back to the user/UI/logs.
    if "token" in lower and any(word in lower for word in ("invalid", "unknown", "expired", "used", "reject", "403")):
        return "Token de unión no válido, caducado o ya usado. Pide una invitación nueva al coordinador."
    if "pubkey is not allowed" in lower or "node pubkey is not allowed" in lower:
        return "Tu clave pública no está autorizada. Pide una invitación nueva al coordinador."
    if "signature verification failed" in lower or "invalid node signature" in lower or "invalid update signature" in lower:
        return "La firma no coincide. Comprueba que usas tu identidad y la invitación correctas."
    if "config fingerprint mismatch" in lower:
        return "La configuración no coincide con la del coordinador. Usa el mismo config que él."
    if "base model fingerprint mismatch" in lower or "different base model" in lower:
        return "El modelo base no coincide. Usa el mismo config/init-state que el coordinador."
    if "timed out" in lower or "quorum" in lower:
        return "El coordinador esperó a más workers. Comprueba que los demás estén conectados."
    if "invite" in lower and ("caduc" in lower or "firma" in lower or "token" in lower):
        return text
    if "cannot reach coordinator" in lower or "connection refused" in lower or "connection error" in lower:
        return "No se puede contactar con el coordinador. Revisa la conexión y el topic de la sala."
    if "late join is disabled" in lower or "run is already finished" in lower or "run already started" in lower:
        return "La sesión ya empezó o terminó. Pide al coordinador que reinicie el run y te invite de nuevo."
    return text
