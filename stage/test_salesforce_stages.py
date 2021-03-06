# Copyright 2017 StreamSets Inc.
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

import copy
import logging

import pytest
from streamsets.testframework.markers import salesforce

logger = logging.getLogger(__name__)

DATA_TO_INSERT = [{'FirstName': 'Test1', 'LastName': 'User1', 'Email': 'xtest1@example.com'},
                  {'FirstName': 'Test2', 'LastName': 'User2', 'Email': 'xtest2@example.com'},
                  {'FirstName': 'Test3', 'LastName': 'User3', 'Email': 'xtest3@example.com'}]
# For testing of SDC-7548
# Since email is used in WHERE clause in lookup processory query,
# create data containing 'from' word in emails to verify the bug is fixed.
DATA_WITH_FROM_IN_EMAIL = [{'FirstName': 'Test1', 'LastName': 'User1', 'Email': 'FROMxtest1@example.com'},
                           {'FirstName': 'Test2', 'LastName': 'User2', 'Email': 'xtefromst2@example.com'},
                           {'FirstName': 'Test3', 'LastName': 'User3', 'Email': 'xtes3@example.comFROM'}]
CSV_DATA_TO_INSERT = [','.join(DATA_TO_INSERT[0].keys())] + [','.join(item.values()) for item in DATA_TO_INSERT]

ACCOUNTS_FOR_SUBQUERY = 5
CONTACTS_FOR_SUBQUERY = 5

@salesforce
def test_salesforce_destination(sdc_builder, sdc_executor, salesforce):
    """
    Send text to Salesforce destination from Dev Raw Data Source and
    confirm that Salesforce destination successfully reads them using Salesforce client.

    The pipeline looks like:
        dev_raw_data_source >> salesforce_destination
    """
    pipeline_builder = sdc_builder.get_pipeline_builder()
    dev_raw_data_source = get_dev_raw_data_source(pipeline_builder, CSV_DATA_TO_INSERT)

    salesforce_destination = pipeline_builder.add_stage('Salesforce', type='destination')
    field_mapping = [{'sdcField': '/FirstName', 'salesforceField': 'FirstName'},
                     {'sdcField': '/LastName', 'salesforceField': 'LastName'},
                     {'sdcField': '/Email', 'salesforceField': 'Email'}]
    salesforce_destination.set_attributes(default_operation='INSERT',
                                          field_mapping=field_mapping,
                                          sobject_type='Contact')

    dev_raw_data_source >> salesforce_destination

    pipeline = pipeline_builder.build(title='Salesforce destination').configure_for_environment(salesforce)
    sdc_executor.add_pipeline(pipeline)
    read_ids = None

    client = salesforce.client
    try:
        # Produce Salesforce records using pipeline.
        logger.info('Starting Salesforce destination pipeline and waiting for it to produce records ...')
        sdc_executor.start_pipeline(pipeline).wait_for_pipeline_batch_count(1)
        sdc_executor.stop_pipeline(pipeline)

        # Using Salesforce connection, read the contents in the Salesforce destination.
        # Changing " with ' and vice versa in following string makes the query execution fail.
        query_str = "SELECT Id, FirstName, LastName, Email FROM Contact WHERE Email LIKE 'xtest%' ORDER BY Id"
        result = client.query(query_str)

        read_data = [f'{item["FirstName"]},{item["LastName"]},{item["Email"]}'
                     for item in result['records']]
        # Following is used later to delete these records.
        read_ids = [{'Id': item['Id']} for item in result['records']]

        assert CSV_DATA_TO_INSERT[1:] == read_data

    finally:
        logger.info('Deleting records ...')
        client.bulk.Contact.delete(read_ids)


@salesforce
@pytest.mark.parametrize(('condition'), [
    'query_without_prefix',
    # Testing of SDC-9067
    'query_with_prefix'
])
def test_salesforce_origin(sdc_builder, sdc_executor, salesforce, condition):
    """
    Create data using Salesforce client
    and then check if Salesforce origin receives them using snapshot.

    The pipeline looks like:
        salesforce_origin >> trash
    """
    pipeline_builder = sdc_builder.get_pipeline_builder()

    if condition == 'query_without_prefix':
        query = ("SELECT Id, FirstName, LastName, Email FROM Contact "
                 "WHERE Id > '000000000000000' AND "
                 "Email LIKE 'xtest%' "
                 "ORDER BY Id")
    else:
        # SDC-9067 - redundant object name prefix caused NPE
        query = ("SELECT Contact.Id, Contact.FirstName, Contact.LastName, Contact.Email FROM Contact "
                 "WHERE Id > '000000000000000' AND "
                 "Email LIKE 'xtest%' "
                 "ORDER BY Id")

    salesforce_origin = pipeline_builder.add_stage('Salesforce', type='origin')
    # Changing " with ' and vice versa in following string makes the query execution fail.
    salesforce_origin.set_attributes(soql_query=query,
                                     subscribe_for_notifications=False)

    trash = pipeline_builder.add_stage('Trash')
    salesforce_origin >> trash
    pipeline = pipeline_builder.build(title='Salesforce Origin').configure_for_environment(salesforce)
    sdc_executor.add_pipeline(pipeline)

    verify_by_snapshot(sdc_executor, pipeline, salesforce_origin, DATA_TO_INSERT, salesforce)


def get_dev_raw_data_source(pipeline_builder, raw_data):
    dev_raw_data_source = pipeline_builder.add_stage('Dev Raw Data Source')
    dev_raw_data_source.set_attributes(data_format='DELIMITED',
                                       header_line='WITH_HEADER',
                                       raw_data='\n'.join(raw_data))
    return dev_raw_data_source


def verify_by_snapshot(sdc_executor, pipeline, stage_name, expected_data, salesforce, data_to_insert=DATA_TO_INSERT):
    client = salesforce.client
    try:
        # Using Salesforce client, create rows in Contact.
        logger.info('Creating rows using Salesforce client ...')
        client.bulk.Contact.insert(data_to_insert)

        logger.info('Starting pipeline and snapshot')
        snapshot = sdc_executor.capture_snapshot(pipeline, start_pipeline=True).snapshot

        rows_from_snapshot = [record.value['value']
                              for record in snapshot[stage_name].output]

        inserted_ids = [dict([(rec['sqpath'].strip('/'), rec['value'])
                             for rec in item if rec['sqpath'] == '/Id'])
                        for item in rows_from_snapshot]

        # Verify correct rows are received using snaphot.
        data_from_snapshot = [dict([(rec['sqpath'].strip('/'), rec['value'])
                                   for rec in item if rec['sqpath'] != '/Id'])
                              for item in rows_from_snapshot]
        assert data_from_snapshot == expected_data

    finally:
        if sdc_executor.get_pipeline_status(pipeline).response.json().get('status') == 'RUNNING':
            logger.info('Stopping pipeline')
            sdc_executor.stop_pipeline(pipeline)
        logger.info('Deleting records ...')
        client.bulk.Contact.delete(inserted_ids)


@salesforce
@pytest.mark.parametrize(('data'), [
    DATA_TO_INSERT,
    # Testing of SDC-7548
    DATA_WITH_FROM_IN_EMAIL
])
def test_salesforce_lookup_processor(sdc_builder, sdc_executor, salesforce, data):
    """Simple Salesforce Lookup processor test.
    Pipeline will enrich records with the 'LastName' of contacts by adding a field as 'surName'.

    The pipeline looks like:
        dev_raw_data_source >> salesforce_lookup >> trash
    """
    pipeline_builder = sdc_builder.get_pipeline_builder()
    lookup_data = ['Email'] + [row['Email'] for row in data]
    dev_raw_data_source = get_dev_raw_data_source(pipeline_builder, lookup_data)

    salesforce_lookup = pipeline_builder.add_stage('Salesforce Lookup')
    # Changing " with ' and vice versa in following string makes the query execution fail.
    query_str = ("SELECT Id, FirstName, LastName FROM Contact "
                 "WHERE Email = '${record:value(\"/Email\")}'")
    field_mappings = [dict(dataType='USE_SALESFORCE_TYPE',
                           salesforceField='LastName',
                           sdcField='/surName')]
    salesforce_lookup.set_attributes(soql_query=query_str,
                                     field_mappings=field_mappings)

    trash = pipeline_builder.add_stage('Trash')
    dev_raw_data_source >> salesforce_lookup >> trash
    pipeline = pipeline_builder.build(title='Salesforce Lookup').configure_for_environment(salesforce)
    sdc_executor.add_pipeline(pipeline)

    LOOKUP_EXPECTED_DATA = copy.deepcopy(data)
    for record in LOOKUP_EXPECTED_DATA:
        record['surName'] = record.pop('LastName')
    verify_by_snapshot(sdc_executor, pipeline, salesforce_lookup, LOOKUP_EXPECTED_DATA,
                       salesforce, data_to_insert=data)

# Test SDC-9251, SDC-9493
@salesforce
@pytest.mark.parametrize(('api'), [
    'soap',
    'bulk'
])
def test_salesforce_origin_subquery(sdc_builder, sdc_executor, salesforce, api):
    """
    Create data using Salesforce client
    and then check if Salesforce origin receives them using snapshot.

    The pipeline looks like:
        salesforce_origin >> trash
    """
    pipeline_builder = sdc_builder.get_pipeline_builder()

    # Also tests SDC-9742 (ORDER BY field causes parse error), SDC-9762 (NPE in sub-query when following relationship)
    # We don't need to populate the ReportsTo field - the error was in retrieving its metadata
    query = ("SELECT Id, Name, (SELECT Id, LastName, ReportsTo.LastName FROM Contacts ORDER BY Id) FROM Account "
             "WHERE Id > '000000000000000' AND "
             "Name LIKE 'Account %' "
             "ORDER BY Id")

    salesforce_origin = pipeline_builder.add_stage('Salesforce', type='origin')
    salesforce_origin.set_attributes(soql_query=query,
                                     use_bulk_api=(api == 'bulk'),
                                     subscribe_for_notifications=False)

    trash = pipeline_builder.add_stage('Trash')
    salesforce_origin >> trash
    pipeline = pipeline_builder.build(title=f'Salesforce Origin Subquery {api}').configure_for_environment(salesforce)
    sdc_executor.add_pipeline(pipeline)

    client = salesforce.client
    try:
        expected_accounts = []
        account_ids = []
        logger.info('Creating accounts using Salesforce client ...')
        for i in range(ACCOUNTS_FOR_SUBQUERY):
            expected_accounts.append({'Name': f'Account {i}'})

        result = client.bulk.Account.insert(expected_accounts)
        account_ids = [{'Id': item['id']}
                       for item in result]

        expected_contacts = []
        contact_ids = []
        logger.info('Creating contacts using Salesforce client ...')
        for i in range(len(expected_accounts)):
            for j in range(CONTACTS_FOR_SUBQUERY):
                contact = {'AccountId': account_ids[i]['Id'], 'LastName': f'Contact {i} {j}'}
                expected_contacts.append(contact)

        result = client.bulk.Contact.insert(expected_contacts)
        contact_ids = [{'Id': item['id']}
                       for item in result]

        logger.info('Starting pipeline and snapshot')
        snapshot = sdc_executor.capture_snapshot(pipeline, start_pipeline=True).snapshot

        # Verify correct rows are received using snapshot.
        assert ACCOUNTS_FOR_SUBQUERY == len(snapshot[salesforce_origin].output)
        for i, account in enumerate(snapshot[salesforce_origin].output):
            assert expected_accounts[i]['Name'] == account.get_field_data('/Name')
            assert CONTACTS_FOR_SUBQUERY == len(account.get_field_data('/Contacts'))
            for j in range(CONTACTS_FOR_SUBQUERY):
                assert expected_contacts[(i * 5) + j]['LastName'] == account.get_field_data(f'/Contacts/{j}/LastName')

    finally:
        if sdc_executor.get_pipeline_status(pipeline).response.json().get('status') == 'RUNNING':
            logger.info('Stopping pipeline')
            sdc_executor.stop_pipeline(pipeline)
        logger.info('Deleting records ...')
        if account_ids:
            client.bulk.Account.delete(account_ids)
        if contact_ids:
            client.bulk.Contact.delete(contact_ids)