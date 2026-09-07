from datetime import datetime, timezone

from sw_bq_loader.transform import table_name_for_query, transform_rows


def test_table_name_is_looker_friendly():
    assert (
        table_name_for_query("DE-ProjectsAdditionalInformation")
        == "de_projects_additional_information"
    )


def test_transform_infers_types_and_protects_ids():
    data = transform_rows(
        [
            {
                "ProjectId": 123,
                "Project Name": "Example",
                "Amount": 12.5,
                "Active": True,
                "Day": "2026-09-04",
            }
        ],
        "agency",
        "DE-Projects",
        "sync-1",
        datetime(2026, 9, 4, tzinfo=timezone.utc),
    )
    schema = {field.name: field.field_type for field in data.schema}
    assert schema["project_id"] == "STRING"
    assert schema["amount"] == "FLOAT"
    assert schema["active"] == "BOOLEAN"
    assert schema["day"] == "DATE"
    assert data.rows[0]["project_id"] == "123"
    assert len(data.rows[0]["sw_row_hash"]) == 64
