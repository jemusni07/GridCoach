"""Hosted-deploy plumbing: service account supplied as an env var instead of a file."""
import base64
import json
import pytest

from app.services import sheets as sheets_mod
from app.services.sheets import SheetsError, SheetsService, _parse_service_account_json

KEY = {"type": "service_account", "client_email": "gridcoach@x.iam.gserviceaccount.com", "private_key": "-----BEGIN-----\nabc\n-----END-----\n"}


def test_parse_accepts_raw_or_base64():
    assert _parse_service_account_json(json.dumps(KEY)) == KEY
    assert _parse_service_account_json(base64.b64encode(json.dumps(KEY).encode()).decode()) == KEY
    with pytest.raises(SheetsError):
        _parse_service_account_json("not json at all")


def test_env_json_takes_precedence_over_missing_file(monkeypatch):
    seen = {}
    monkeypatch.setattr(sheets_mod.gspread, "service_account_from_dict", lambda info, **kw: (seen.__setitem__("info", info), "CLIENT")[1])
    svc = SheetsService("/definitely/missing.json", json.dumps(KEY))
    assert svc.gc == "CLIENT" and seen["info"]["client_email"] == KEY["client_email"]
    # file path alone, missing → clear error naming both options
    with pytest.raises(SheetsError, match="GOOGLE_SERVICE_ACCOUNT_JSON"):
        SheetsService("/definitely/missing.json").gc
