# imports
import django
import hashlib
from inspect import isfunction, signature
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

# import from core.spark
try:
	from utils import PythonUDFRecord, refresh_django_db_connection	
except:
	from core.spark.utils import PythonUDFRecord, refresh_django_db_connection

# init django settings file to retrieve settings
os.environ['DJANGO_SETTINGS_MODULE'] = 'combine.settings'
sys.path.append('/opt/combine')
django.setup()

from django.conf import settings
from django.db import connection

# import select models from Core
from core.models import Job, ValidationScenario



####################################################################
# Record Validation												   #
####################################################################

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

		# init logging support
		spark.sparkContext.setLogLevel('INFO')
		log4jLogger = spark.sparkContext._jvm.org.apache.log4j
		self.logger = log4jLogger.LogManager.getLogger(__name__)


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

		# refresh Django DB Connection
		refresh_django_db_connection()

		# loop through validation scenarios and fire validation type specific method
		for vs_id in self.validation_scenarios:

			# get validation scenario
			vs = ValidationScenario.objects.get(pk=vs_id)
			vs_id = vs.id
			vs_filepath = vs.filepath

			# schematron based validation scenario
			if vs.validation_type == 'sch':
				validation_fails_rdd = self._sch_validation(vs, vs_id, vs_filepath)

			# python based validation scenario
			elif vs.validation_type == 'python':
				validation_fails_rdd = self._python_validation(vs, vs_id, vs_filepath)

			# finally, write to DB if validation failures
			if not validation_fails_rdd.isEmpty():
				validation_fails_rdd.toDF().write.jdbc(
					settings.COMBINE_DATABASE['jdbc_url'],
					'core_recordvalidation',
					properties=settings.COMBINE_DATABASE,
					mode='append')

	
	def _sch_validation(self, vs, vs_id, vs_filepath):

		self.logger.info('running schematron validation: %s' % vs.name)

		def validate_schematron_pt_udf(pt):

			# parse schematron
			sct_doc = etree.parse(vs_filepath)
			validator = isoschematron.Schematron(sct_doc, store_report=True)

			for row in pt:

				# get document xml
				record_xml = etree.fromstring(row.document.encode('utf-8'))

				# validate
				is_valid = validator.validate(record_xml)

				# if not valid, prepare Row
				if not is_valid:

					# prepare results_dict
					results_dict = {
						'fail_count':0,
						'failed':[]
					}

					# get failed
					report_root = validator.validation_report.getroot()
					fails = report_root.findall('svrl:failed-assert', namespaces=report_root.nsmap)

					# log fail_count
					results_dict['fail_count'] = len(fails)

					# loop through fails and add to dictionary
					for fail in fails:
						fail_text_elem = fail.find('svrl:text', namespaces=fail.nsmap)
						results_dict['failed'].append(fail_text_elem.text)
					
					yield Row(
						record_id=int(row.id),
						validation_scenario_id=int(vs_id),
						valid=0,
						results_payload=json.dumps(results_dict),
						fail_count=results_dict['fail_count']
					)

		# run pt_udf map
		validation_fails_rdd = self.records_df.rdd.mapPartitions(validate_schematron_pt_udf).filter(lambda row: row is not None)

		# return
		return validation_fails_rdd


	def _python_validation(self, vs, vs_id, vs_filepath):

		self.logger.info('running python validation: %s' % vs.name)

		def validate_python_udf(vs_id, pyvs_funcs, row):

			'''
			Loop through test functions and aggregate in fail_dict to return with Row

			Args:
				vs_id (int): integer of validation scenario
				pyvs_funcs (list): list of functions imported from user created python validation scenario payload
				row (): 
			'''

			# prepare row as parsed document with PythonUDFRecord class
			prvb = PythonUDFRecord(row)
			
			# prepare results_dict
			results_dict = {
				'fail_count':0,
				'failed':[]
			}

			# loop through functions
			for func in pyvs_funcs:

				# get name as string
				func_name = func.__name__

				# get func test message
				func_signature = signature(func)
				t_msg = func_signature.parameters['test_message'].default

				# attempt to run user-defined validation function
				try:

					# run test
					test_result = func(prvb)

					# if fail, append
					if test_result != True:

						# bump fail count
						results_dict['fail_count'] += 1

						# if custom message override provided, use
						if test_result != False:
							results_dict['failed'].append(test_result)

						# else, default to test message
						else:
							results_dict['failed'].append(t_msg)

				# if problem, report as failure with Exception string
				except Exception as e:
					results_dict['fail_count'] += 1
					results_dict['failed'].append("test '%s' had exception: %s" % (func_name, str(e)))

			# if failed, return Row
			if results_dict['fail_count'] > 0:

				# return row			
				return Row(
					record_id=int(row.id),
					validation_scenario_id=int(vs_id),
					valid=0,
					results_payload=json.dumps(results_dict),
					fail_count=results_dict['fail_count']
				)

		# parse user defined functions from validation scenario payload
		temp_pyvs = ModuleType('temp_pyvs')
		exec(vs.payload, temp_pyvs.__dict__)

		# get defined functions
		pyvs_funcs = []
		test_labeled_attrs = [ attr for attr in dir(temp_pyvs) if attr.lower().startswith('test') ]
		for attr in test_labeled_attrs:
			attr = getattr(temp_pyvs, attr)
			if isfunction(attr):
				pyvs_funcs.append(attr)

		validation_fails_rdd = self.records_df.rdd.\
			map(lambda row: validate_python_udf(vs_id, pyvs_funcs, row))\
			.filter(lambda row: row is not None)

		# return
		return validation_fails_rdd












