import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app import main


def _configure_store(tmp_path, monkeypatch):
    alarms = tmp_path / "Full_alarms.csv"
    alarms.write_text(
        "sep=;\n"
        "Order;Type;Category;Location;Classification;Comment;Message\n"
        "1,002;Alarm (A);Out-of-bound;source.c:10.1-4;;;bad index\n",
        encoding="utf-8",
    )
    variables = tmp_path / "variables.json"
    variables.write_text(
        json.dumps(
            [
                {
                    "group_id": 1,
                    "location": "source.c:10.1-4",
                    "variable": "items[i]",
                    "no_of_paths": 1,
                    "paths": [
                        {
                            "path_id": 1,
                            "function_sequence": ["main", "read_items"],
                            "call_stack": ["call#main at source.c:1.1-2"],
                        }
                    ],
                    "variable_info": {
                        "array_name": "items",
                        "occurrence_count": 1,
                        "occurrences": [
                            {"location": "source.c:10.1-4", "access": "read"}
                        ],
                    },
                    "variable_infos": [],
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        main, "_store", main.AlarmStore(alarms, variables)
    )


def test_list_and_lookup(tmp_path, monkeypatch):
    _configure_store(tmp_path, monkeypatch)
    client = TestClient(main.app)

    response = client.get("/api/alarms")
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["order_id"] == "1,002"

    response = client.get("/api/alarm/1002")
    assert response.status_code == 200
    assert response.json()["variable"] == "items[i]"

    response = client.get("/api/alarm", params={"order": "1,002"})
    assert response.status_code == 200
    assert response.json()["variable"] == "items[i]"


def test_missing_alarm_is_not_found(tmp_path, monkeypatch):
    _configure_store(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/api/alarm/999")
    assert response.status_code == 404


def test_alarm_uses_cfg_when_call_stack_missing(tmp_path, monkeypatch):
    alarms = tmp_path / "Full_alarms.csv"
    alarms.write_text(
        "sep=;\n"
        "Order;Type;Category;Location;Classification;Comment;Message\n"
        "2,989;Alarm (A);Out-of-bound;source.c:10.1-4;;;\n",
        encoding="utf-8",
    )
    variables = tmp_path / "variables.json"
    variables.write_text(
        json.dumps(
            [
                {
                    "location": "source.c:10.1-4",
                    "variable": None,
                    "paths": [{"path_id": 1, "function_sequence": [], "call_stack": []}],
                    "variable_info": {
                        "array_name": "items",
                        "index_expression": "i",
                        "used_in_functions": ["read_items"],
                        "occurrences": [{"line": 10, "function": "read_items"}],
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    graph = {"main": ["read_items"], "read_items": ["helper"], "helper": []}
    monkeypatch.setattr(main, "_store", main.AlarmStore(alarms, variables))
    monkeypatch.setattr(main, "_cfg_graph", graph)
    monkeypatch.setattr(main, "_cfg_callers", main._index_callers(graph))
    monkeypatch.setattr(main, "_active_pver", "5901")
    response = TestClient(main.app).get("/api/alarm/2989")
    assert response.status_code == 200
    payload = response.json()
    assert payload["variable"] == "items[i]"
    assert payload["cfg_neighborhood"]["function"] == "read_items"
    assert payload["cfg_neighborhood"]["callers"] == ["main"]
    assert payload["cfg_neighborhood"]["callees"] == ["helper"]
    assert payload["graph_highlight"]["center"] == "read_items"
    assert "main" in payload["graph_highlight"]["cf_nodes"]
    assert "read_items" in payload["graph_highlight"]["symbol_nodes"]
    assert payload["variable_info"]["used_in_functions_ordered"][-1] == "read_items"


def test_graph_highlight_includes_every_unique_path(tmp_path, monkeypatch):
    alarms = tmp_path / "Full_alarms.csv"
    alarms.write_text(
        "sep=;\n"
        "Order;Type;Category;Location;Classification;Comment;Message\n"
        "53;Alarm (A);Out-of-bound;source.c:10.1-4;;;\n",
        encoding="utf-8",
    )
    variables = tmp_path / "variables.json"
    variables.write_text(
        json.dumps(
            [
                {
                    "location": "source.c:10.1-4",
                    "enclosing_function": "leaf",
                    "paths": [
                        {"function_sequence": ["main|separate", "mid", "leaf"]},
                        {"function_sequence": ["boot|separate", "mid", "leaf"]},
                        {"function_sequence": ["main|separate", "mid", "leaf"]},
                    ],
                    "variable_info": {
                        "array_name": "items",
                        "used_in_functions": ["leaf"],
                        "occurrences": [{"function": "leaf", "access": "read"}],
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    graph = {"main": ["mid"], "boot": ["mid"], "mid": ["leaf"], "leaf": []}
    monkeypatch.setattr(main, "_store", main.AlarmStore(alarms, variables))
    monkeypatch.setattr(main, "_cfg_graph", graph)
    monkeypatch.setattr(main, "_cfg_callers", main._index_callers(graph))
    monkeypatch.setattr(main, "_active_pver", "4105")
    payload = TestClient(main.app).get("/api/alarm/53").json()["graph_highlight"]
    assert payload["path_count"] == 2
    assert ["main", "mid", "leaf"] in payload["function_sequences"]
    assert ["boot", "mid", "leaf"] in payload["function_sequences"]
    assert set(payload["cf_nodes"]) == {"main", "boot", "mid", "leaf"}
    assert ["main", "mid"] in payload["cf_edges"]
    assert ["boot", "mid"] in payload["cf_edges"]


def test_dataflow_functions_follow_cfg_order(monkeypatch):
    graph = {"main": ["mid"], "mid": ["leaf"], "other": ["mid"], "leaf": []}
    monkeypatch.setattr(main, "_cfg_graph", graph)
    monkeypatch.setattr(main, "_cfg_callers", main._index_callers(graph))
    ordered = main._order_functions_by_cfg(
        ["leaf", "global", "other", "main", "mid"],
        leaf="leaf",
        path=["main", "mid", "leaf"],
    )
    assert ordered[0] == "global"
    assert ordered[-1] == "leaf"
    assert ordered.index("main") < ordered.index("mid") < ordered.index("leaf")


def test_list_pvers(tmp_path, monkeypatch):
    root = tmp_path / "PVERs"
    sample = root / "4105"
    sample.mkdir(parents=True)
    (sample / "Full_alarms.csv").write_text(
        "sep=;\nOrder;Type;Category;Location;Classification;Comment;Message\n"
        "1;Alarm (A);Out-of-bound;x.c:1.1-2;;;oob\n",
        encoding="utf-8",
    )
    (sample / "input.c").write_text("int a[2];\n", encoding="utf-8")
    monkeypatch.setattr(main, "PVER_ROOT", root)
    monkeypatch.setattr(main, "_store", None)
    monkeypatch.setattr(main, "_active_pver", None)
    response = TestClient(main.app).get("/api/pvers")
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["id"] == "4105"
    assert payload["items"][0]["has_alarms"] is True
    assert payload["items"][0]["has_source"] is True
    assert payload["items"][0]["has_cfg"] is False
    assert payload["items"][0]["status"] == "needs_build"


def test_open_pver_graph_uses_only_that_folder_cfg(tmp_path, monkeypatch):
    root = tmp_path / "PVERs"
    first = root / "4105"
    second = root / "5901"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    (first / "full_control_flow_graph.json").write_text(
        '{"alpha": ["beta"], "beta": []}',
        encoding="utf-8",
    )
    (second / "full_control_flow_graph.json").write_text(
        '{"omega": ["zeta"], "zeta": []}',
        encoding="utf-8",
    )
    monkeypatch.setattr(main, "PVER_ROOT", root)
    monkeypatch.setattr(main, "_store", None)
    monkeypatch.setattr(main, "_cfg_graph", None)
    monkeypatch.setattr(main, "_cfg_callers", None)
    monkeypatch.setattr(main, "_full_graph", None)
    monkeypatch.setattr(main, "_active_pver", None)
    client = TestClient(main.app)

    opened = client.post("/api/pvers/4105/open")
    assert opened.status_code == 200
    assert opened.json()["status"] == "ready"
    assert opened.json()["graph_file"] == "full_control_flow_graph.json"

    summary = client.get("/api/cfg/summary")
    assert summary.status_code == 200
    payload = summary.json()
    assert payload["pver"] == "4105"
    assert payload["file"] == "full_control_flow_graph.json"
    assert payload["path"].replace("\\", "/").endswith("PVERs/4105/full_control_flow_graph.json")
    names = {item["name"] for item in payload["hubs"]}
    assert names == {"alpha", "beta"}
    assert "omega" not in names

    neighborhood = client.get("/api/cfg/neighborhood", params={"fn": "alpha"})
    assert neighborhood.status_code == 200
    assert neighborhood.json()["callees"] == ["beta"]

    opened = client.post("/api/pvers/5901/open")
    assert opened.status_code == 200
    summary = client.get("/api/cfg/summary")
    names = {item["name"] for item in summary.json()["hubs"]}
    assert names == {"omega", "zeta"}
    assert "alpha" not in names


def test_list_pver_is_ready_from_cfg_json_only(tmp_path, monkeypatch):
    root = tmp_path / "PVERs"
    sample = root / "4105"
    sample.mkdir(parents=True)
    (sample / "full_control_flow_graph.json").write_text(
        '{"main": ["helper"], "helper": []}',
        encoding="utf-8",
    )
    monkeypatch.setattr(main, "PVER_ROOT", root)
    monkeypatch.setattr(main, "_store", None)
    monkeypatch.setattr(main, "_active_pver", None)
    response = TestClient(main.app).get("/api/pvers")
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["id"] == "4105"
    assert item["has_cfg"] is True
    assert item["cfg_file"] == "full_control_flow_graph.json"
    assert item["status"] == "ready"


def test_control_flow_graph_lists_every_caller_and_callee():
    repo = Path(__file__).resolve().parents[3]
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    import array_oob_analyzer as analyzer

    source = """
static inline __attribute__ ((always_inline)) void Inner(int x)
{
    helper(x);
}

void helper(int x);

int foo(int a)
{
    helper(a);
    return bar(
        a
    );
}

int bar(int a)
{
    return (*callback)(a);
}

void unused(void)
{
}

int (*callback)(int);
"""
    graph = analyzer.build_control_flow_graph(source)
    assert graph["Inner"] == ["helper"]
    assert graph["foo"] == ["helper", "bar"]
    assert graph["bar"] == ["callback"]
    assert graph["unused"] == []
    assert graph["helper"] == []
    assert graph["callback"] == []
    assert "always_inline" not in graph
    assert "attribute" not in graph


def test_csv_analysis_merges_all_message_log_paths(tmp_path):
    repo = Path(__file__).resolve().parents[3]
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    import array_oob_analyzer as analyzer

    (tmp_path / "Full_alarms.csv").write_text(
        "sep=;\n"
        "Order;Type;Category;Location;Classification;Comment;Message\n"
        "1;Alarm (A);Out-of-bound array access;src.c:10.1-4;;;ALARM (A) array_out_of_bounds: bad at src.c:10.1-4\n",
        encoding="utf-8",
    )
    (tmp_path / "messeges.txt").write_text(
        "[ call#main at src.c:1.1-2\n"
        "  call#read_items at src.c:8.1-4\n"
        "  ALARM (A) array_out_of_bounds: bad at src.c:10.1-4 ]\n"
        "> items[i]\n"
        ">      ~\n"
        "[ call#boot at src.c:2.1-2\n"
        "  call#read_items at src.c:8.1-4\n"
        "  ALARM (A) array_out_of_bounds: bad at src.c:10.1-4 ]\n"
        "> items[i]\n"
        ">      ~\n"
        "[ call#main at src.c:1.1-2\n"
        "  call#read_items at src.c:8.1-4\n"
        "  ALARM (A) array_out_of_bounds: bad at src.c:10.1-4 ]\n"
        "> items[i]\n"
        ">      ~\n",
        encoding="utf-8",
    )
    (tmp_path / "input.c").write_text("int items[2];\nvoid read_items(void) { items[1]; }\n", encoding="utf-8")
    output = tmp_path / "array_oob_variable_info.json"
    old = sys.argv
    sys.argv = [
        "array_oob_analyzer.py",
        str(tmp_path / "Full_alarms.csv"),
        str(tmp_path / "input.c"),
        str(output),
        str(tmp_path / "full_control_flow_graph.json"),
    ]
    try:
        analyzer.main()
    finally:
        sys.argv = old
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload[0]["no_of_paths"] == 2
    assert payload[0]["no_of_traces"] == 3
    sequences = [tuple(path["function_sequence"]) for path in payload[0]["paths"]]
    assert ("main", "read_items") in sequences
    assert ("boot", "read_items") in sequences
