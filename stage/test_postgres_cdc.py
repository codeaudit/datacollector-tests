# Copyright 2018 StreamSets Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import string
from collections import namedtuple

import pytest
import sqlalchemy
from streamsets.testframework.markers import database, sdc_min_version
from streamsets.testframework.utils import get_random_string

logger = logging.getLogger(__name__)

PRIMARY_KEY = 'id'
NAME_COLUMN = 'name'
OperationsData = namedtuple('OperationsData', ['kind', 'table', 'columnnames', 'columnvalues', 'oldkeys'])
Oldkeys = namedtuple('Oldkeys', ['keynames', 'keyvalues'])

INSERT_ROWS = [
    {PRIMARY_KEY: 0, NAME_COLUMN: 'Mbappe'},
    {PRIMARY_KEY: 1, NAME_COLUMN: 'Kane'},
    {PRIMARY_KEY: 2, NAME_COLUMN: 'Griezmann'}
]
UPDATE_ROWS = [
    {PRIMARY_KEY: 0, NAME_COLUMN: 'Kylian Mbappe'},
    {PRIMARY_KEY: 1, NAME_COLUMN: 'Harry Kane'},
    {PRIMARY_KEY: 2, NAME_COLUMN: 'Antoine Griezmann'}
]
DELETE_ROWS = [{PRIMARY_KEY: 0}, {PRIMARY_KEY: 1}, {PRIMARY_KEY: 2}]
KIND_FOR_INSERT = 'insert'
KIND_FOR_UPDATE = 'update'
KIND_FOR_DELETE = 'delete'


def _create_table_in_database(table_name, database):
    metadata = sqlalchemy.MetaData()
    table = sqlalchemy.Table(
        table_name,
        metadata,
        sqlalchemy.Column(PRIMARY_KEY, sqlalchemy.Integer, primary_key=True),
        sqlalchemy.Column(NAME_COLUMN, sqlalchemy.String(20))
    )
    logger.info('Creating table %s in %s database ...', table_name, database.type)
    table.create(database.engine)
    return table


def _insert(connection, table):
    connection.execute(table.insert(), INSERT_ROWS)

    # Prepare expected data to compare for verification against snapshot data.
    operations_data = []
    for row in INSERT_ROWS:
        operations_data.append(OperationsData(KIND_FOR_INSERT,
                                              table.name,
                                              [PRIMARY_KEY, NAME_COLUMN],
                                              list(row.values()),
                                              None))  # No oldkeys expected.
    return operations_data


def _update(connection, table):
    txn = connection.begin()
    try:
        for row in UPDATE_ROWS:
            connection.execute(table.update().where(table.c.id == row[PRIMARY_KEY]).values(name=row[NAME_COLUMN]))
        txn.commit()
    except:
        txn.rollback()
        raise

    # Prepare expected data to compare for verification against snapshot data.
    operations_data = []
    for row in UPDATE_ROWS:
        operations_data.append(OperationsData(KIND_FOR_UPDATE,
                                              table.name,
                                              [PRIMARY_KEY, NAME_COLUMN],
                                              list(row.values()),
                                              Oldkeys([PRIMARY_KEY], [row[PRIMARY_KEY]])))
    return operations_data


def _delete(connection, table):
    txn = connection.begin()
    try:
        for row in DELETE_ROWS:
            connection.execute(table.delete().where(table.c.id == row[PRIMARY_KEY]))
        txn.commit()
    except:
        txn.rollback()
        raise

    # Prepare expected data to compare for verification against snapshot data.
    operations_data = []
    for row in DELETE_ROWS:
        operations_data.append(OperationsData(KIND_FOR_DELETE,
                                              table.name,
                                              None,  # No columnnames expected.
                                              None,  # No columnvalues expected.
                                              Oldkeys([PRIMARY_KEY], [row[PRIMARY_KEY]])))
    return operations_data


@database('postgresql')
@sdc_min_version('3.4.0')
def test_postgres_cdc_client_basic(sdc_builder, sdc_executor, database):
    """Basic test that inserts/updates/deletes to a Postgres table,
    and validates that they are read in the same order.
    Here `Initial Change` config. is at default value = `From the latest change`.
    With this, the origin processes all changes that occur after pipeline is started.

    The pipeline looks like:
        postgres_cdc_client >> trash
    """
    if not database.is_cdc_enabled:
        pytest.skip('Test only runs against PostgreSQL with CDC enabled.')

    table_name = get_random_string(string.ascii_lowercase, 20)

    pipeline_builder = sdc_builder.get_pipeline_builder()
    postgres_cdc_client = pipeline_builder.add_stage('PostgreSQL CDC Client')
    postgres_cdc_client.set_attributes(remove_replication_slot_on_close=True,
                                       replication_slot=get_random_string(string.ascii_lowercase, 10))
    trash = pipeline_builder.add_stage('Trash')
    postgres_cdc_client >> trash

    pipeline = pipeline_builder.build(title='PostgreSQL CDC Basic').configure_for_environment(database)
    sdc_executor.add_pipeline(pipeline)

    try:
        # Database operations done after pipeline start will be captured by CDC.
        # Hence start the pipeline but do not wait for the capture to be finished.
        snapshot_command = sdc_executor.capture_snapshot(pipeline, start_pipeline=True, wait=False)

        # Create table and then perform insert, update and delete operations.
        table = _create_table_in_database(table_name, database)
        connection = database.engine.connect()
        expected_operations_data = _insert(connection=connection, table=table)
        expected_operations_data += _update(connection=connection, table=table)
        expected_operations_data += _delete(connection=connection, table=table)

        snapshot = snapshot_command.wait_for_finished().snapshot

        # Verify snapshot data is received in exact order as expected.
        operation_index = 0
        for record in snapshot[postgres_cdc_client.instance_name].output:
            # No need to worry about DDL related CDC records. e.g. table creation etc.
            if record.get_field_data('/change'):
                # Since we performed each operation (insert, update and delete) on 3 rows,
                # each CDC  record change contains a list of 3 elements.
                for i in range(3):
                    expected = expected_operations_data[operation_index]
                    assert expected.kind == record.get_field_data(f'/change/[{i}]/kind')
                    assert expected.table == record.get_field_data(f'/change/[{i}]/table')
                    # For delete operation there are no columnnames and columnvalues fields.
                    if expected.kind != KIND_FOR_DELETE:
                        assert expected.columnnames == record.get_field_data(f'/change/[{i}]/columnnames')
                        assert expected.columnvalues == record.get_field_data(f'/change/[{i}]/columnvalues')
                    if expected.kind != KIND_FOR_INSERT:
                        # For update and delete operations verify extra information about old keys.
                        assert expected.oldkeys.keynames == record.get_field_data(f'/change/[{i}]/oldkeys/keynames')
                        assert expected.oldkeys.keyvalues == record.get_field_data(f'/change/[{i}]/oldkeys/keyvalues')
                    operation_index += 1

    finally:
        if pipeline:
            sdc_executor.stop_pipeline(pipeline=pipeline, force=True)
        if table is not None:
            table.drop(database.engine)
            logger.info('Table: %s dropped.', table_name)
