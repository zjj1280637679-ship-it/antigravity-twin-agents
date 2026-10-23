import json
from pathlib import Path

def test_plugin_json_exists():
    root_dir = Path(__file__).parent.parent
    plugin_json_path = root_dir / 'plugin.json'
    assert plugin_json_path.exists(), "plugin.json does not exist at project root"

def test_plugin_json_is_valid_and_has_required_fields():
    root_dir = Path(__file__).parent.parent
    plugin_json_path = root_dir / 'plugin.json'
    
    with open(plugin_json_path, 'r', encoding='utf-8') as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            assert False, f"plugin.json is not valid JSON: {e}"
            
    assert 'name' in data, "plugin.json is missing 'name' field"
    assert 'version' in data, "plugin.json is missing 'version' field"
