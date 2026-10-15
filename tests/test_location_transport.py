"""An explicitly unavailable Core cannot silently select another authority."""
import os
from pathlib import Path
import sys
import pytest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'addons/autopilot-mode/skill/adapters'))
from autopilot_work_items import _load_core_ledger_module


@pytest.mark.parametrize('root_kind',('absolute','relative','broken'))
def test_invalid_inherited_core_does_not_reuse_warm_provider_or_ancestor(tmp_path,root_kind):
    core=Path(os.environ['GHOST_ALICE_CORE_ROOT'])
    locator=tmp_path/'ledger/codex/location-fixture/intent-state.json'
    assert _load_core_ledger_module(locator,{'GHOST_ALICE_CORE_ROOT':str(core)}) is not None
    invalid=str(core/'.tmp/missing-location-provider')
    if root_kind=='relative':invalid='relative-unavailable-core'
    elif root_kind=='broken':
        root=tmp_path/'broken-provider';script=root/'session-intent-analyzer/scripts/session_intent_ledger.py'
        script.parent.mkdir(parents=True);script.write_text("raise RuntimeError('fixture-only broken provider')\n")
        invalid=str(root)
    with patch.dict(os.environ,{'GHOST_ALICE_CORE_ROOT':invalid}):
        assert _load_core_ledger_module(locator,{}) is None


@pytest.mark.parametrize('root_kind',('missing','whitespace'))
def test_inherited_unavailable_core_is_typed_by_current_projection(tmp_path,root_kind):
    from autopilot_runtime_context import read_session_material
    from autopilot_intent_recovery import current_execution_projection
    from response_route_fixture import native_ledger
    core=Path(os.environ['GHOST_ALICE_CORE_ROOT'])
    root=tmp_path/'ledger'
    provider=_load_core_ledger_module(root/'codex/session-1/intent-state.json',{'GHOST_ALICE_CORE_ROOT':str(core)})
    # This constructed public-intake fixture is never native-host evidence.
    native_ledger(root,provider)
    source=dict(GHOST_ALICE_PLATFORM='codex',GHOST_ALICE_SESSION_ID='session-1',
        GHOST_ALICE_SESSION_INTENT_ROOT=str(root),GHOST_ALICE_COMPLETION_SCHEMA='v2')
    material=read_session_material(root,'codex','session-1',source=dict(source,GHOST_ALICE_CORE_ROOT=str(core)))
    before=provider.read_session_state(root=root,platform='codex',session_id='session-1')
    run=tmp_path/'run'
    invalid=str(core/'.tmp/missing-location-provider') if root_kind=='missing' else '\t \n'
    with patch.dict(os.environ,{'GHOST_ALICE_CORE_ROOT':invalid}):
        result=current_execution_projection(material,source,run)
    assert result['status']=='recording-unavailable'
    assert result['reason']=='core-route-api-unavailable'
    assert provider.read_session_state(root=root,platform='codex',session_id='session-1')==before
    assert not run.exists()


@pytest.mark.parametrize('selection',('source','environment'))
@pytest.mark.parametrize('raw',('\n\t ',))
def test_nonempty_whitespace_root_is_unavailable_after_warm_provider(tmp_path,selection,raw):
    core=Path(os.environ['GHOST_ALICE_CORE_ROOT'])
    locator=tmp_path/'ledger/intent-state.json'
    assert _load_core_ledger_module(locator,{'GHOST_ALICE_CORE_ROOT':str(core)}) is not None
    supplied={'GHOST_ALICE_CORE_ROOT':raw} if selection=='source' else {}
    inherited=str(core/'.tmp/missing-location-provider') if selection=='source' else raw
    with patch.dict(os.environ,{'GHOST_ALICE_CORE_ROOT':inherited}):
        assert _load_core_ledger_module(locator,supplied) is None


def test_ordinary_root_precedence_and_empty_clear_discovery_are_preserved(tmp_path):
    core=Path(os.environ['GHOST_ALICE_CORE_ROOT'])
    missing=str(core/'.tmp/missing-location-provider')
    # Discovery needs an actual known provider ancestor once explicit location is cleared.
    locator=core/'.tmp/location-fixture/ledger/intent-state.json'
    for supplied,inherited,available in (
            ({'GHOST_ALICE_CORE_ROOT':str(core)},missing,True),
            ({'GHOST_ALICE_CORE_ROOT':missing},str(core),False),
            ({},None,True),({},'',True),({'GHOST_ALICE_CORE_ROOT':''},None,True),
            ({'GHOST_ALICE_CORE_ROOT':''},missing,False)):
        with patch.dict(os.environ,{},clear=False):
            if inherited is None:os.environ.pop('GHOST_ALICE_CORE_ROOT',None)
            else:os.environ['GHOST_ALICE_CORE_ROOT']=inherited
            provider=_load_core_ledger_module(locator,supplied)
            assert (provider is not None)==available
            if available:
                assert Path(provider.__file__).resolve()==core/'session-intent-analyzer/scripts/session_intent_ledger.py'


def test_invalid_explicit_core_does_not_fallback_from_ledger_ancestor():
    core = Path(os.environ['GHOST_ALICE_CORE_ROOT'])
    assert _load_core_ledger_module(core/'.tmp/location-red/intent-state.json',{'GHOST_ALICE_CORE_ROOT':str(core/'missing')}) is None


def test_ap_descriptor_write_rejects_escape_before_creating_parent(tmp_path):
    import pytest
    from unittest.mock import patch
    import autopilot_storage as storage
    owned=tmp_path/'owned';owned.mkdir()
    outside=tmp_path/'outside'/'authority.json'
    with patch.dict(os.environ,{'GHOST_ALICE_OWNED_ROOT':str(owned)}):
        with pytest.raises(ValueError,match='escape'):
            storage._atomic(outside,{'schema_version':'fixture-only'})
    assert not outside.parent.exists()
