from __future__ import annotations

import json
from pathlib import Path


def test_content_studio_workflow_generates_planned_media():
    path = Path("n8n-workflows/13-content-studio-pipeline.json")
    workflow = json.loads(path.read_text(encoding="utf-8"))

    nodes = {node["name"]: node for node in workflow["nodes"]}

    assert "Generate planned media" in nodes
    generate_node = nodes["Generate planned media"]
    assert "/generate-plan" in generate_node["parameters"]["url"]
    assert "image" in generate_node["parameters"]["jsonBody"]
    assert "voice" in generate_node["parameters"]["jsonBody"]
    assert "avatar" in generate_node["parameters"]["jsonBody"]
    assert "animation" in generate_node["parameters"]["jsonBody"]

    connections = workflow["connections"]
    assert "Asset plan ready?" in connections
    assert "Generate planned media" in json.dumps(connections["Asset plan ready?"])
