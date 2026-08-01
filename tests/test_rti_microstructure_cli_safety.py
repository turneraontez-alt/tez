from __future__ import annotations

import sys

import pytest

from tools import q15_rti_microstructure_feature_audit as feature_audit
from tools import q15_rti_microstructure_freeze as freeze
from tools import q15_rti_microstructure_preregister as preregister


@pytest.mark.parametrize(
    "module",
    (feature_audit, freeze, preregister),
)
def test_version_sensitive_cli_requires_explicit_design(
    module, monkeypatch, capsys,
):
    monkeypatch.setattr(sys, "argv", [module.__file__])
    with pytest.raises(SystemExit) as exc:
        module.main()
    assert exc.value.code == 2
    assert "--design" in capsys.readouterr().err
