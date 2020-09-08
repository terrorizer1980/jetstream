from google.cloud import bigquery
import pytest

from jetstream.logging import setup_logger, logger


class TestLoggingIntegration:
    @pytest.fixture(autouse=True)
    def logging_table_setup(self, client, temporary_dataset, project_id):
        schema = [
            bigquery.SchemaField("submission_timestamp", "TIMESTAMP"),
            bigquery.SchemaField("experiment", "STRING"),
            bigquery.SchemaField("message", "STRING"),
            bigquery.SchemaField("log_level", "STRING"),
            bigquery.SchemaField("exception", "STRING"),
            bigquery.SchemaField("filename", "STRING"),
            bigquery.SchemaField("func_name", "STRING"),
            bigquery.SchemaField("exception_type", "STRING"),
        ]

        table = bigquery.Table(f"{project_id}.{temporary_dataset}.logs", schema=schema)
        table = client.client.create_table(table)

        setup_logger(project_id, temporary_dataset, "logs", log_to_bigquery=True, capacity=1)

        yield
        client.client.delete_table(table, not_found_ok=True)

    def test_logging_to_bigquery(self, client, temporary_dataset, project_id):
        logger.info("Do not write to BigQuery")
        logger.warning("Write warning to Bigquery")
        logger.error("Write error to BigQuery", extra={"experiment": "test_experiment"})
        logger.exception(
            "Write exception to BigQuery",
            exc_info=Exception("Some exception"),
            extra={"experiment": "test_experiment"},
        )

        result = list(
            client.client.query(f"SELECT * FROM {project_id}.{temporary_dataset}.logs").result()
        )
        assert any([r.message == "Write warning to Bigquery" for r in result])
        assert (
            any([r.message == "Do not write to BigQuery" and r.log_level == "WARN" for r in result])
            is False
        )
        assert any(
            [
                r.message == "Write error to BigQuery"
                and r.experiment == "test_experiment"
                and r.log_level == "ERROR"
                for r in result
            ]
        )
        assert any(
            [
                r.message == "Write exception to BigQuery"
                and r.experiment == "test_experiment"
                and r.log_level == "ERROR"
                and "Exception('Some exception')" in r.exception
                for r in result
            ]
        )
