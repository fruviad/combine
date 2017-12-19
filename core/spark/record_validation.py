# imports
import django
import hashlib
from inspect import isfunction
import json
from lxml import etree, isoschematron
import os
import shutil
import sys
from types import ModuleType

# import Row from pyspark
from pyspark.sql import Row
from pyspark.sql.types import StringType, IntegerType
from pyspark.sql.functions import udf

# init django settings file to retrieve settings
os.environ['DJANGO_SETTINGS_MODULE'] = 'combine.settings'
sys.path.append('/opt/combine')
django.setup()
from django.conf import settings

# import select models from Core
from core.models import Job, PythonRecordValidationBase, ValidationScenario




class ValidationScenarioSpark(object):

	'''
	Class to organize methods and attributes used for running validation scenarios
	'''

	def __init__(self, spark=None, job=None, records_df=None, validation_scenarios=None):

		'''
		Args:
			spark (pyspark.sql.session.SparkSession): spark instance from static job methods
			job (core.models.Job): Job instance		
			records_df (pyspark.sql.DataFrame): records as pyspark DataFrame
			validation_scenarios (list): list of ValidationScenario job ids as integers
		'''

		self.spark = spark
		self.job = job
		self.records_df = records_df
		self.validation_scenarios = validation_scenarios


	def run_record_validation_scenarios(self):

		'''
		Function to run validation scenarios
		Results are written to RecordValidation table, one result, per record, per failed validation test.

		Validation tests may be of type:
			- 'sch': Schematron based validation, performed with lxml etree
			- 'python': custom python code snippets

		Args:
			None

		Returns:
			None
				- writes validation fails to RecordValidation table
		'''

		# loop through validation scenarios and fire validation type specific method
		for vs_id in self.validation_scenarios:

			# get validation scenario
			vs = ValidationScenario.objects.get(pk=vs_id)
			vs_id = vs.id
			vs_filepath = vs.filepath

			# schematron based validation scenario
			if vs.validation_type == 'sch':

				# run udf map
				udf_func = self.validate_schematron_udf # must pass static method not attached to self
				validation_fails_rdd = self.records_df.rdd.\
					map(lambda row: udf_func(vs_id, vs_filepath, row))\
					.filter(lambda row: row is not None)


			# python based validation scenario
			elif vs.validation_type == 'python':

				# parse user defined functions from validation scenario payload
				temp_pyvs = ModuleType('temp_pyvs')
				exec(vs.payload, temp_pyvs.__dict__)

				# get defined functions
				pyvs_funcs = []
				for attr in dir(temp_pyvs):
					attr = getattr(temp_pyvs, attr)
					if isfunction(attr):
						pyvs_funcs.append(attr)

				udf_func = self.validate_python_udf # must pass static method not attached to self				
				validation_fails_rdd = self.records_df.rdd.\
					map(lambda row: udf_func(vs_id, pyvs_funcs, row))\
					.filter(lambda row: row is not None)


			# finally, write to DB if validation failures
			if not validation_fails_rdd.isEmpty():
				validation_fails_rdd.toDF().write.jdbc(
					settings.COMBINE_DATABASE['jdbc_url'],
					'core_recordvalidation',
					properties=settings.COMBINE_DATABASE,
					mode='append')


	@staticmethod
	def validate_schematron_udf(vs_id, vs_filepath, row):

			# parse schematron
			sct_doc = etree.parse(vs_filepath)
			validator = isoschematron.Schematron(sct_doc, store_report=True)

			# get document xml
			record_xml = etree.fromstring(row.document.encode('utf-8'))

			# validate
			is_valid = validator.validate(record_xml)

			# if not valid, prepare Row
			if not is_valid:

				# prepare fail_dict
				fail_dict = {
					'count':0,
					'failures':[]
				}

				# get failures
				report_root = validator.validation_report.getroot()
				fails = report_root.findall('svrl:failed-assert', namespaces=report_root.nsmap)

				# log count
				fail_dict['count'] = len(fails)

				# loop through fails and add to dictionary
				for fail in fails:
					fail_text_elem = fail.find('svrl:text', namespaces=fail.nsmap)
					fail_dict['failures'].append(fail_text_elem.text)
				
				return Row(
					record_id=int(row.id),
					validation_scenario_id=int(vs_id),
					valid=0,
					results_payload=json.dumps(fail_dict),
					fail_count=fail_dict['count']
				)


	@staticmethod
	def validate_python_udf(vs_id, pyvs_funcs, row):

		'''
		Loop through test functions and aggregate in fail_dict to return with Row

		Args:
			vs_id (int): integer of validation scenario
			pyvs_funcs (list): list of functions imported from user created python validation scenario payload
			row (): 
		'''
		
		# prepare fail_dict
		fail_dict = {
			'count':0,
			'failures':[]
		}

		# loop through functions
		for func in pyvs_funcs:

			# run test
			test_result = func(row)

			# if fail, append
			if test_result != True:
				fail_dict['count'] += 1
				fail_dict['failures'].append(test_result)

		# return row
		return Row(
			record_id=int(row.id),
			validation_scenario_id=int(vs_id),
			valid=0,
			results_payload=json.dumps(fail_dict),
			fail_count=fail_dict['count']
		)
















