import json

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


def test_missing_alarm_is_not_found(tmp_path, monkeypatch):
    _configure_store(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/api/alarm/999")
    assert response.status_code == 404
