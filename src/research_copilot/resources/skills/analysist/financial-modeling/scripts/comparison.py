"""Comparable company table, retaining explicit periods/units and input values."""

from io_contract import cli, comparable, number, text


def calculate(companies, metrics, sort_by=None, sort_order="desc"):
    entities = [company["entity"] for company in companies]
    if (
        not 1 <= len(entities) <= 12
        or len(set(entities)) != len(entities)
        or not 1 <= len(metrics) <= 30
    ):
        raise ValueError("Bounded unique companies and metrics required")
    rows = []
    for metric in metrics:
        values = metric["values"]
        if {v["entity"] for v in values} != set(entities) or len(values) != len(
            entities
        ):
            raise ValueError("Every company requires one value")
        comparable(
            [v["value"] for v in values], ("unit", "currency", "period", "basis")
        )
        if any(v["entity"] != v["value"]["context"]["entity"] for v in values):
            raise ValueError("Company identity mismatch")
        rows.append(
            {
                "name": metric["name"],
                "values": {v["entity"]: text(number(v["value"])) for v in values},
                "unit": values[0]["value"]["context"]["unit"],
            }
        )
    if sort_order not in {"asc", "desc"}:
        raise ValueError("Invalid sort order")
    if sort_by:
        row = next((r for r in rows if r["name"] == sort_by), None)
        if row is None:
            raise ValueError("Unknown sort metric")
        from io_contract import decimal

        entities.sort(
            key=lambda entity: decimal(row["values"][entity]),
            reverse=sort_order == "desc",
        )
    return {"entities": entities, "rows": rows}


if __name__ == "__main__":
    cli(calculate)
