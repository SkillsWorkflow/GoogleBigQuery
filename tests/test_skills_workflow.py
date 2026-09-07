from sw_bq_loader.skills_workflow import _infer_order_field, _parse_page


def test_parse_page_accepts_data_wrapper():
    page = _parse_page({"data": [{"Id": "1"}], "totalCount": 1})
    assert page.rows == [{"Id": "1"}]
    assert page.total_count == 1


def test_infer_order_field_prefers_exact_id():
    assert _infer_order_field([{"ProjectId": "p", "Id": "1"}]) == "Id"
