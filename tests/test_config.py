import json

import pytest

from sw_bq_loader.config import Credentials, QueryConfig, load_queries


def test_credentials_accept_camel_case_aliases():
    value = json.dumps(
        {
            "tenant": "p3k-br",
            "tenantId": "tenant-id",
            "appId": "app-id",
            "appSecret": "secret",
        }
    )
    credentials = Credentials.from_json(value)
    assert credentials.tenant == "p3k-br"
    assert credentials.app_id == "app-id"
    assert credentials.base_url == "https://apiv2-p3k-br.skillsworkflow.com"


def test_credentials_reject_invalid_tenant():
    with pytest.raises(ValueError):
        Credentials.from_json(
            json.dumps(
                {"tenant": "../bad", "tenantId": "x", "appId": "y", "appSecret": "z"}
            )
        )


def test_query_config_validates_filters():
    with pytest.raises(ValueError):
        QueryConfig.from_dict({"name": "DE-Projects", "filters": "not-an-array"})


def test_query_overrides_can_add_custom_query(tmp_path):
    path = tmp_path / "queries.json"
    path.write_text('[{"name":"DE-Projects","refresh_minutes":60}]', encoding="utf-8")
    queries = load_queries(str(path), '{"Agency-Custom":{"refresh_minutes":15}}')
    assert [query.name for query in queries] == ["DE-Projects", "Agency-Custom"]
    assert queries[1].refresh_minutes == 15
