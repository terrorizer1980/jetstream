import datetime as dt
from textwrap import dedent

import mozanalysis.segments
import pytest
import pytz
import toml

from jetstream import AnalysisPeriod, config
from jetstream.config import PLATFORM_CONFIGS
from jetstream.experimenter import Experiment
from jetstream.pre_treatment import CensorHighestValues, Log, RemoveNulls
from jetstream.statistics import BootstrapMean

DEFAULT_METRICS_CONFIG = PLATFORM_CONFIGS["firefox_desktop"].config_spec_path


class TestAnalysisSpec:
    def test_trivial_configuration(self, experiments):
        spec = config.AnalysisSpec.from_dict({})
        assert isinstance(spec, config.AnalysisSpec)
        cfg = spec.resolve(experiments[0])
        assert isinstance(cfg, config.AnalysisConfiguration)
        assert cfg.experiment.segments == []

    def test_scalar_metrics_throws_exception(self):
        config_str = dedent(
            """
            [metrics]
            weekly = "my_cool_metric"
            """
        )
        with pytest.raises(ValueError):
            config.AnalysisSpec.from_dict(toml.loads(config_str))

    def test_template_expansion(self, experiments):
        config_str = dedent(
            """
            [metrics]
            weekly = ["my_cool_metric"]
            [metrics.my_cool_metric]
            data_source = "main"
            select_expression = "{{agg_histogram_mean('payload.content.my_cool_histogram')}}"

            [metrics.my_cool_metric.statistics.bootstrap_mean]
            """
        )
        spec = config.AnalysisSpec.from_dict(toml.loads(config_str))
        cfg = spec.resolve(experiments[0])
        metric = [m for m in cfg.metrics[AnalysisPeriod.WEEK] if m.metric.name == "my_cool_metric"][
            0
        ].metric
        assert "agg_histogram_mean" not in metric.select_expr
        assert "json_extract_histogram" in metric.select_expr

    def test_recognizes_metrics(self, experiments):
        config_str = dedent(
            """
            [metrics]
            weekly = ["view_about_logins"]

            [metrics.view_about_logins.statistics.bootstrap_mean]
            """
        )
        spec = config.AnalysisSpec.from_dict(toml.loads(config_str))
        cfg = spec.resolve(experiments[0])
        assert (
            len(
                [
                    m
                    for m in cfg.metrics[AnalysisPeriod.WEEK]
                    if m.metric.name == "view_about_logins"
                ]
            )
            == 1
        )

    def test_duplicate_metrics_are_okay(self, experiments):
        config_str = dedent(
            """
            [metrics]
            weekly = ["unenroll", "unenroll", "active_hours"]

            [metrics.unenroll.statistics.binomial]
            [metrics.active_hours.statistics.bootstrap_mean]
            """
        )
        spec = config.AnalysisSpec.from_dict(toml.loads(config_str))
        cfg = spec.resolve(experiments[0])
        assert (
            len([m for m in cfg.metrics[AnalysisPeriod.WEEK] if m.metric.name == "unenroll"]) == 1
        )

    def test_data_source_definition(self, experiments):
        config_str = dedent(
            """
            [metrics]
            weekly = ["spam", "taunts"]
            [metrics.spam]
            data_source = "eggs"
            select_expression = "1"
            [metrics.spam.statistics.bootstrap_mean]

            [metrics.taunts]
            data_source = "silly_knight"
            select_expression = "1"
            [metrics.taunts.statistics.bootstrap_mean]

            [data_sources.eggs]
            from_expression = "england.camelot"
            client_id_column = "client_info.client_id"

            [data_sources.silly_knight]
            from_expression = "france"
            experiments_column_type = "none"
            """
        )
        spec = config.AnalysisSpec.from_dict(toml.loads(config_str))
        cfg = spec.resolve(experiments[0])
        spam = [m for m in cfg.metrics[AnalysisPeriod.WEEK] if m.metric.name == "spam"][0].metric
        taunts = [m for m in cfg.metrics[AnalysisPeriod.WEEK] if m.metric.name == "taunts"][
            0
        ].metric
        assert spam.data_source.name == "eggs"
        assert "camelot" in spam.data_source._from_expr
        assert "client_info" in spam.data_source.client_id_column
        assert spam.data_source.experiments_column_type == "simple"
        assert taunts.data_source.experiments_column_type is None

    def test_definitions_override_other_metrics(self, experiments):
        """Test that config definitions override mozanalysis definitions.
        Users can specify a metric with the same name as a metric built into mozanalysis.
        The user's metric from the config file should win.
        """
        config_str = dedent(
            """
            [metrics]
            weekly = ["active_hours"]
            """
        )
        default_spec = config.AnalysisSpec.from_dict(toml.load(DEFAULT_METRICS_CONFIG))
        spec = config.AnalysisSpec.from_dict(toml.loads(config_str))
        spec.merge(default_spec)
        cfg = spec.resolve(experiments[0])
        stock = [m for m in cfg.metrics[AnalysisPeriod.WEEK] if m.metric.name == "active_hours"][
            0
        ].metric

        config_str = dedent(
            """
            [metrics]
            weekly = ["active_hours"]
            [metrics.active_hours]
            select_expression = "spam"
            data_source = "main"

            [metrics.active_hours.statistics.bootstrap_mean]
            """
        )
        spec = config.AnalysisSpec.from_dict(toml.loads(config_str))
        cfg = spec.resolve(experiments[0])
        custom = [m for m in cfg.metrics[AnalysisPeriod.WEEK] if m.metric.name == "active_hours"][
            0
        ].metric

        assert stock != custom
        assert custom.select_expr == "spam"
        assert stock.select_expr != custom.select_expr

    def test_unknown_statistic_failure(self, experiments):
        config_str = dedent(
            """
            [metrics]
            weekly = ["spam"]

            [metrics.spam]
            data_source = "main"
            select_expression = "1"

            [metrics.spam.statistics.unknown_stat]
            """
        )

        with pytest.raises(ValueError) as e:
            spec = config.AnalysisSpec.from_dict(toml.loads(config_str))
            spec.resolve(experiments[0])

        assert "Statistic unknown_stat does not exist" in str(e)

    def test_overwrite_statistic(self, experiments):
        config_str = dedent(
            """
            [metrics]
            weekly = ["spam"]

            [metrics.spam]
            data_source = "main"
            select_expression = "1"

            [metrics.spam.statistics.bootstrap_mean]
            num_samples = 10
            """
        )

        spec = config.AnalysisSpec.from_dict(toml.loads(config_str))
        cfg = spec.resolve(experiments[0])
        bootstrap_mean = [m for m in cfg.metrics[AnalysisPeriod.WEEK] if m.metric.name == "spam"][
            0
        ].statistic
        bootstrap_mean.__class__ = BootstrapMean

        assert bootstrap_mean.num_samples == 10

    def test_overwrite_default_statistic(self, experiments):
        config_str = dedent(
            """
            [metrics]
            weekly = ["ad_clicks"]

            [metrics.ad_clicks.statistics.bootstrap_mean]
            num_samples = 10
            """
        )

        default_spec = config.AnalysisSpec.from_dict(toml.load(DEFAULT_METRICS_CONFIG))
        spec = config.AnalysisSpec.from_dict(toml.loads(config_str))
        default_spec.merge(spec)
        cfg = default_spec.resolve(experiments[0])
        bootstrap_mean = [
            m for m in cfg.metrics[AnalysisPeriod.WEEK] if m.metric.name == "ad_clicks"
        ][0].statistic
        bootstrap_mean.__class__ = BootstrapMean

        assert bootstrap_mean.num_samples == 10

    def test_pre_treatment(self, experiments):
        config_str = dedent(
            """
            [metrics]
            weekly = ["spam"]

            [metrics.spam]
            data_source = "main"
            select_expression = "1"

            [metrics.spam.statistics.bootstrap_mean]
            num_samples = 10
            pre_treatments = ["remove_nulls"]
            """
        )

        spec = config.AnalysisSpec.from_dict(toml.loads(config_str))
        cfg = spec.resolve(experiments[0])
        pre_treatments = [m for m in cfg.metrics[AnalysisPeriod.WEEK] if m.metric.name == "spam"][
            0
        ].pre_treatments

        assert len(pre_treatments) == 1
        assert pre_treatments[0].__class__ == RemoveNulls

    def test_invalid_pre_treatment(self, experiments):
        config_str = dedent(
            """
            [metrics]
            weekly = ["spam"]

            [metrics.spam]
            data_source = "main"
            select_expression = "1"

            [metrics.spam.statistics.bootstrap_mean]
            num_samples = 10
            pre_treatments = [
                {name = "not_existing"}
            ]
            """
        )

        with pytest.raises(ValueError) as e:
            spec = config.AnalysisSpec.from_dict(toml.loads(config_str))
            spec.resolve(experiments[0])

        assert "Could not find pre-treatment not_existing." in str(e)

    def test_merge_configs(self, experiments):
        orig_conf = dedent(
            """
            [metrics]
            weekly = ["spam"]

            [metrics.spam]
            data_source = "main"
            select_expression = "1"

            [metrics.spam.statistics.bootstrap_mean]
            num_samples = 10
            """
        )

        custom_conf = dedent(
            """
            [metrics]
            weekly = ["foo"]

            [metrics.foo]
            data_source = "main"
            select_expression = "2"

            [metrics.foo.statistics.bootstrap_mean]
            num_samples = 100
            """
        )

        spec = config.AnalysisSpec.from_dict(toml.loads(orig_conf))
        spec.merge(config.AnalysisSpec.from_dict(toml.loads(custom_conf)))
        cfg = spec.resolve(experiments[0])

        assert len(cfg.metrics[AnalysisPeriod.WEEK]) == 2
        assert len([m for m in cfg.metrics[AnalysisPeriod.WEEK] if m.metric.name == "spam"]) == 1
        assert len([m for m in cfg.metrics[AnalysisPeriod.WEEK] if m.metric.name == "foo"]) == 1

    def test_merge_configs_override_metric(self, experiments):
        orig_conf = dedent(
            """
            [metrics]
            weekly = ["spam"]

            [metrics.spam]
            data_source = "main"
            select_expression = "1"

            [metrics.spam.statistics.bootstrap_mean]
            num_samples = 10
            """
        )

        custom_conf = dedent(
            """
            [metrics]
            weekly = ["spam"]

            [metrics.spam]
            data_source = "events"
            select_expression = "2"

            [metrics.spam.statistics.bootstrap_mean]
            num_samples = 100
            """
        )

        spec = config.AnalysisSpec.from_dict(toml.loads(orig_conf))
        spec.merge(config.AnalysisSpec.from_dict(toml.loads(custom_conf)))
        cfg = spec.resolve(experiments[0])

        spam = [m for m in cfg.metrics[AnalysisPeriod.WEEK] if m.metric.name == "spam"][0]

        assert len(cfg.metrics[AnalysisPeriod.WEEK]) == 1
        assert spam.metric.data_source.name == "events"
        assert spam.metric.select_expr == "2"
        assert spam.statistic.name() == "bootstrap_mean"
        assert spam.statistic.num_samples == 100


class TestExperimentSpec:
    def test_null_query(self, experiments):
        spec = config.AnalysisSpec.from_dict({})
        cfg = spec.resolve(experiments[0])
        assert cfg.experiment.enrollment_query is None

    def test_trivial_query(self, experiments):
        conf = dedent(
            """
            [experiment]
            enrollment_query = "SELECT 1"
            """
        )
        spec = config.AnalysisSpec.from_dict(toml.loads(conf))
        cfg = spec.resolve(experiments[0])
        assert cfg.experiment.enrollment_query == "SELECT 1"

    def test_template_query(self, experiments):
        conf = dedent(
            """
            [experiment]
            enrollment_query = "SELECT 1 FROM foo WHERE slug = '{{experiment.experimenter_slug}}'"
            """
        )
        spec = config.AnalysisSpec.from_dict(toml.loads(conf))
        cfg = spec.resolve(experiments[0])
        assert cfg.experiment.enrollment_query == "SELECT 1 FROM foo WHERE slug = 'test_slug'"

    def test_silly_query(self, experiments):
        conf = dedent(
            """
            [experiment]
            enrollment_query = "{{experiment.enrollment_query}}"  # whoa
            """
        )
        spec = config.AnalysisSpec.from_dict(toml.loads(conf))
        with pytest.raises(ValueError):
            spec.resolve(experiments[0])

    def test_control_branch(self, experiments):
        trivial = config.AnalysisSpec().resolve(experiments[0])
        assert trivial.experiment.reference_branch == "b"

        conf = dedent(
            """
            [experiment]
            reference_branch = "a"
            """
        )
        spec = config.AnalysisSpec.from_dict(toml.loads(conf))
        configured = spec.resolve(experiments[0])
        assert configured.experiment.reference_branch == "a"

    def test_recognizes_segments(self, experiments):
        conf = dedent(
            """
            [experiment]
            segments = ["regular_users_v3"]
            """
        )
        spec = config.AnalysisSpec.from_dict(toml.loads(conf))
        configured = spec.resolve(experiments[0])
        assert isinstance(configured.experiment.segments[0], mozanalysis.segments.Segment)

    def test_segment_definitions(self, experiments):
        conf = dedent(
            """
            [experiment]
            segments = ["regular_users_v3", "my_cool_segment"]

            [segments.my_cool_segment]
            data_source = "my_cool_data_source"
            select_expression = "{{agg_any('1')}}"

            [segments.data_sources.my_cool_data_source]
            from_expression = "(SELECT 1 WHERE submission_date BETWEEN {{experiment.start_date_str}} AND {{experiment.last_enrollment_date_str}})"
            """  # noqa
        )

        spec = config.AnalysisSpec.from_dict(toml.loads(conf))
        configured = spec.resolve(experiments[0])
        assert len(configured.experiment.segments) == 2
        for segment in configured.experiment.segments:
            assert isinstance(segment, mozanalysis.segments.Segment)
        assert configured.experiment.segments[0].name == "regular_users_v3"
        assert configured.experiment.segments[1].name == "my_cool_segment"
        assert "agg_any" not in configured.experiment.segments[1].select_expr
        assert "1970" not in configured.experiment.segments[1].data_source._from_expr
        assert "{{" not in configured.experiment.segments[1].data_source._from_expr
        assert "2019-12-01" in configured.experiment.segments[1].data_source._from_expr

    def test_pre_treatment_config(self, experiments):
        config_str = dedent(
            """
            [metrics]
            weekly = ["spam"]

            [metrics.spam]
            data_source = "main"
            select_expression = "1"

            [metrics.spam.statistics.bootstrap_mean]
            num_samples = 10
            pre_treatments = [
                {name = "remove_nulls"},
                {name = "log", base = 20.0},
                {name = "censor_highest_values", fraction = 0.9}
            ]
            """
        )

        spec = config.AnalysisSpec.from_dict(toml.loads(config_str))
        cfg = spec.resolve(experiments[0])
        pre_treatments = [m for m in cfg.metrics[AnalysisPeriod.WEEK] if m.metric.name == "spam"][
            0
        ].pre_treatments

        assert len(pre_treatments) == 3
        assert pre_treatments[0].__class__ == RemoveNulls
        assert pre_treatments[1].__class__ == Log
        assert pre_treatments[2].__class__ == CensorHighestValues

        assert pre_treatments[1].base == 20.0
        assert pre_treatments[2].fraction == 0.9

    def test_pre_treatment_config_multiple_periods(self, experiments):
        config_str = dedent(
            """
            [metrics]
            weekly = ["spam"]
            overall = ["spam"]

            [metrics.spam]
            data_source = "main"
            select_expression = "1"

            [metrics.spam.statistics.binomial]
            pre_treatments = ["remove_nulls"]
            """
        )

        spec = config.AnalysisSpec.from_dict(toml.loads(config_str))
        cfg = spec.resolve(experiments[0])
        pre_treatments = [m for m in cfg.metrics[AnalysisPeriod.WEEK] if m.metric.name == "spam"][
            0
        ].pre_treatments

        assert len(pre_treatments) == 1
        assert pre_treatments[0].__class__ == RemoveNulls

        overall_pre_treatments = [
            m for m in cfg.metrics[AnalysisPeriod.OVERALL] if m.metric.name == "spam"
        ][0].pre_treatments

        assert len(overall_pre_treatments) == 1
        assert overall_pre_treatments[0].__class__ == RemoveNulls


class TestExperimentConf:
    def test_bad_dates(self, experiments):
        conf = dedent(
            """
            [experiment]
            end_date = "Christmas"
            """
        )
        with pytest.raises(ValueError):
            config.AnalysisSpec.from_dict(toml.loads(conf))

        conf = dedent(
            """
            [experiment]
            start_date = "My birthday"
            """
        )
        with pytest.raises(ValueError):
            config.AnalysisSpec.from_dict(toml.loads(conf))

    def test_good_end_date(self, experiments):
        conf = dedent(
            """
            [experiment]
            end_date = "2020-12-31"
            """
        )
        spec = config.AnalysisSpec.from_dict(toml.loads(conf))
        live_experiment = [x for x in experiments if x.status == "Live"][0]
        cfg = spec.resolve(live_experiment)
        assert cfg.experiment.end_date == dt.datetime(2020, 12, 31, tzinfo=pytz.utc)
        assert cfg.experiment.status == "Complete"

    def test_good_start_date(self, experiments):
        conf = dedent(
            """
            [experiment]
            start_date = "2020-12-31"
            end_date = "2021-02-01"
            """
        )
        spec = config.AnalysisSpec.from_dict(toml.loads(conf))
        cfg = spec.resolve(experiments[0])
        assert cfg.experiment.start_date == dt.datetime(2020, 12, 31, tzinfo=pytz.utc)
        assert cfg.experiment.end_date == dt.datetime(2021, 2, 1, tzinfo=pytz.utc)


class TestDefaultConfiguration:
    def test_descriptions_defined(self, experiments):
        default_spec = config.AnalysisSpec.from_dict(toml.load(DEFAULT_METRICS_CONFIG))
        cfg = default_spec.resolve(experiments[0])
        ever_ran = False
        for summaries in cfg.metrics.values():
            for summary in summaries:
                ever_ran = True
                assert summary.metric.friendly_name
                assert summary.metric.description
        assert ever_ran


class TestFenixConfiguration:
    def test_default_metrics(self, fenix_experiments):
        for experiment in fenix_experiments:
            default = config.AnalysisSpec.default_for_experiment(experiment).resolve(experiment)
            found = False
            for summary in default.metrics[AnalysisPeriod.WEEK]:
                if summary.metric.data_source.name == "baseline":
                    found = True
            assert found


class TestOutcomes:
    def test_outcomes(self):
        config_str = dedent(
            """
            friendly_name = "Test outcome"
            description = "Outcome for testing"

            [metrics.spam]
            data_source = "main"
            select_expression = "1"

            [metrics.spam.statistics.bootstrap_mean]
            num_samples = 10
            pre_treatments = ["remove_nulls"]

            [metrics.organic_search_count.statistics.bootstrap_mean]

            [data_sources.eggs]
            from_expression = "england.camelot"
            client_id_column = "client_info.client_id"
            """
        )

        outcome_spec = config.OutcomeSpec.from_dict(toml.loads(config_str))
        assert "spam" in outcome_spec.metrics
        assert "organic_search_count" in outcome_spec.metrics
        assert "eggs" in outcome_spec.data_sources.definitions

    def test_resolving_outcomes(self, experiments, fake_outcome_resolver):
        config_str = dedent(
            """
            [metrics]
            weekly = ["view_about_logins", "my_cool_metric"]
            daily = ["my_cool_metric"]

            [metrics.my_cool_metric]
            data_source = "main"
            select_expression = "{{agg_histogram_mean('payload.content.my_cool_histogram')}}"
            friendly_name = "Cool metric"
            description = "Cool cool cool 😎"
            bigger_is_better = false

            [metrics.my_cool_metric.statistics.bootstrap_mean]

            [metrics.view_about_logins.statistics.bootstrap_mean]
            """
        )

        spec = config.AnalysisSpec.from_dict(toml.loads(config_str))
        cfg = spec.resolve(experiments[5])

        weekly_metrics = [s.metric.name for s in cfg.metrics[AnalysisPeriod.WEEK]]
        assert "view_about_logins" in weekly_metrics
        assert "my_cool_metric" in weekly_metrics
        assert "meals_eaten" in weekly_metrics
        assert "speed" in weekly_metrics

    def test_unsupported_platform_outcomes(self, experiments, fake_outcome_resolver):
        spec = config.AnalysisSpec.from_dict(toml.loads(""))
        experiment = Experiment(
            experimenter_slug="test_slug",
            type="pref",
            status="Live",
            start_date=dt.datetime(2019, 12, 1, tzinfo=pytz.utc),
            end_date=dt.datetime(2020, 3, 1, tzinfo=pytz.utc),
            proposed_enrollment=7,
            branches=[],
            normandy_slug="normandy-test-slug",
            reference_branch=None,
            is_high_population=True,
            outcomes=["performance"],
            app_name="fenix",
            app_id="org.mozilla.fenix",
        )

        with pytest.raises(ValueError):
            spec.resolve(experiment)
