'''Firefox Desktop Search Count Datasets

This job produces derived datasets that make it easy to explore search count
data.

The search_aggregates job is used to populate an executive search dashboard.
For more information, see Bug 1381140.

The search_clients_daily job produces a dataset keyed by
`(client_id, submission_date, search_counts.engine, search_counts.source)`.
This allows for deeper analysis into user level search behavior.
'''
import click
import logging
import datetime
from pyspark.sql.functions import explode, col, when, udf, sum
from pyspark.sql.types import StringType
from pyspark.sql import SparkSession


DEFAULT_INPUT_BUCKET = 'telemetry-parquet'
DEFAULT_INPUT_PREFIX = 'main_summary/v4'
DEFAULT_SAVE_MODE = 'error'
MAX_CLIENT_SEARCH_COUNT = 10000

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def search_aggregates(main_summary):
    return agg_search_data(
        main_summary,
        [
            'addon_version',
            'app_version',
            'country',
            'distribution_id',
            'engine',
            'locale',
            'search_cohort',
            'source',
            'submission_date',
        ],
        []
    )


def agg_search_data(main_summary, grouping_cols, agg_functions):
    """Augment, Explode, and Aggregate search data

    The augmented and exploded dataset has the same columns as main_summary
    with the addition of the following:

        engine: A key in the search_counts field representing a search engine.
                e.g. 'hoolie'
        source: A key in the search_counts field representing a search source
                e.g. 'urlbar'
        tagged-sap: Sum of all searches with partner codes from an SAP
        tagged-follow-on: Sum of all searches with partner codes from a downstream query
        sap: Sum of all searches originating from a direct user interaction with the Firefox UI
        addon_version: The version of the followon-search@mozilla.com addon
    """

    exploded = explode_search_counts(main_summary)
    augmented = add_derived_columns(exploded)

    # Do all aggregations
    aggregated = (
        augmented
        .groupBy(grouping_cols + ['type'])
        .agg(*(agg_functions + [sum('count').alias('count')]))
    )

    # Pivot on search type
    pivoted = (
        aggregated
        .groupBy([col for col in aggregated.columns if col not in ['type', 'count']])
        .pivot(
            'type',
            ['tagged-sap', 'tagged-follow-on', 'sap']
        )
        .sum('count')
    )

    return pivoted


def get_search_addon_version(active_addons):
    if not active_addons:
        return None
    return next((a[5] for a in active_addons if a[0] == "followonsearch@mozilla.com"),
                None)


def explode_search_counts(main_summary):
    exploded_col_name = 'single_search_count'
    search_fields = [exploded_col_name + '.' + field
                     for field in ['engine', 'source', 'count']]

    return (
        main_summary
        .withColumn(exploded_col_name, explode(col('search_counts')))
        .select(['*'] + search_fields)
        .filter('single_search_count.count < %s' % MAX_CLIENT_SEARCH_COUNT)
        .drop(exploded_col_name)
        .drop('search_counts')
    )


def add_derived_columns(exploded_search_counts):
    '''Adds the following columns to the provided dataset:

    type:           One of 'in-content-sap', 'follow-on', or 'chrome-sap'.
    addon_version:  The version of the followon-search@mozilla addon, or None
    '''
    udf_get_search_addon_version = udf(get_search_addon_version, StringType())

    return (
        exploded_search_counts
        .withColumn(
            'type',
            when(col('source').startswith('sap:'), 'tagged-sap')
            .otherwise(
                when(col('source').startswith('follow-on:'), 'tagged-follow-on')
                .otherwise('sap')
            )
        )
        .withColumn('addon_version', udf_get_search_addon_version('active_addons'))
    )


def generate_dashboard(submission_date, bucket, prefix,
                       input_bucket=DEFAULT_INPUT_BUCKET,
                       input_prefix=DEFAULT_INPUT_PREFIX,
                       save_mode=DEFAULT_SAVE_MODE):
    start = datetime.datetime.now()
    spark = (
        SparkSession
        .builder
        .appName('search_dashboard_etl')
        .getOrCreate()
    )

    version = 3
    source_path = 's3://{}/{}/submission_date_s3={}'.format(
        input_bucket,
        input_prefix,
        submission_date
    )
    output_path = 's3://{}/{}/v{}/submission_date_s3={}'.format(
        bucket,
        prefix,
        version,
        submission_date
    )

    logger.info('Loading main_summary...')
    main_summary = spark.read.parquet(source_path)

    logger.info('Running the search dashboard ETL job...')
    search_dashboard_data = search_aggregates(main_summary)

    logger.info('Saving rollups to: {}'.format(output_path))
    (
        search_dashboard_data
        .repartition(10)
        .write
        .mode(save_mode)
        .save(output_path)
    )

    spark.stop()
    logger.info('... done (took: %s)', str(datetime.datetime.now() - start))


@click.command()
@click.option('--submission_date', required=True)
@click.option('--bucket', required=True)
@click.option('--prefix', required=True)
@click.option('--input_bucket',
              default=DEFAULT_INPUT_BUCKET,
              help='Bucket of the input dataset')
@click.option('--input_prefix',
              default=DEFAULT_INPUT_PREFIX,
              help='Prefix of the input dataset')
@click.option('--save_mode',
              default=DEFAULT_SAVE_MODE,
              help='Save mode for writing data')
def main(submission_date, bucket, prefix, input_bucket, input_prefix,
         save_mode):
    generate_dashboard(submission_date, bucket, prefix, input_bucket,
                       input_prefix, save_mode)
