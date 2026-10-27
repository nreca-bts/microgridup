import random, json, os, csv
from random import randint
from datetime import datetime as dt
from datetime import timedelta as td
import pandas as pd
from numpy.random import exponential
import plotly.graph_objects as go
import microgridup
from omf.solvers import opendss
from omf.solvers.opendss import dssConvert
from omf.models.outageCost import stats
from omf.models.microgridControl import customerCost1, utilityOutageTable


def main(in_csv, data, in_dss, out_html):
	'''
	Output for resilience before/after microgrid deployment.

	:param in_csv: the path of the outage CSV that was created via _many_random_outages()
	:type csv_path: str
	:param data: the data which contains the microgrid definitions
	:type data: dict
	:param in_dss: the path of the circuit file that contains the microgrids
	:type in_dss: str
	:param out_html: the path of the HTML file that will be created in this function
	:type out_html: str
	:rtype: None
	'''
	outages = pd.read_csv(in_csv)
	# - Step 1: filter out outages that didn't affect any meters because those shouldn't affect the resilience statistics
	outages = outages[~pd.isna(outages['Meters Affected'])]
	# - Step 2: split up every meter within a single row of the "Meters Affected" column into its own row with only a single meter
	outages['Meters Affected'] = outages['Meters Affected'].apply(lambda meters: meters.split())
	atomic_outages = outages.explode('Meters Affected')
	# - Step 3: identify meter location and criticality

	def identify_microgrid(meter):
		microgrid_name = None
		for mg_name, mg in data['MICROGRIDS'].items():
			if meter in mg['loads']:
				microgrid_name = mg_name
		return microgrid_name

	atomic_outages['Microgrid'] = atomic_outages['Meters Affected'].apply(identify_microgrid)
	atomic_outages['Criticality'] = atomic_outages['Meters Affected'].apply(lambda meter: meter in data['CRITICAL_LOADS'])
	# - Step 4: split meters into 4 categories
	noncritical_without_mg = atomic_outages[(atomic_outages['Criticality'] == False) & (pd.isna(atomic_outages['Microgrid']))]
	noncritical_mg = atomic_outages[(atomic_outages['Criticality'] == False) & (~pd.isna(atomic_outages['Microgrid']))]
	critical_without_mg = atomic_outages[(atomic_outages['Criticality'] == True) & (pd.isna(atomic_outages['Microgrid']))]
	critical_mg = atomic_outages[(atomic_outages['Criticality'] == True) & (~pd.isna(atomic_outages['Microgrid']))]
	# - Step 5: adjust the outage duration for each meter within a microgrid based on the outage location and the average outage survival duration of
	#   that microgrid
	mg_average_hours_survived = {}
	for mg_name in data['MICROGRIDS'].keys():
		with open(f'reopt_{mg_name}/allOutputData.json') as f:
			mg_average_hours_survived[mg_name] = json.load(f)['avgOutage1']
	tree = dssConvert.dssToTree(in_dss)

	def adjust_outage_duration(series):
		# - Don't mutate the passed object
		series = series.copy()
		sub_lines = opendss.get_subtree_lines(data['MICROGRIDS'][series['Microgrid']]['switch'], tree)
		if series['ComponentAff'] in [line['object'].split('.')[1] for line in sub_lines]:
			# - If the affected component was in the microgrid, then the microgrid would not have mitigated the outage, so don't do anything
			pass
		else:
			# - If the affected component was not in the microgrid, then the microgrid would have mitigated the outage, so adjust the outage duration
			duration_hours = (pd.Timestamp(series['Finish']) - pd.Timestamp(series['Start'])).total_seconds() / 3600
			difference = duration_hours - mg_average_hours_survived[series['Microgrid']]
			if difference > 0:
				# - If the outage duration was longer than the average microgrid survival time, then shorten the outage
				series['Start'] = str((pd.Timestamp(series['Finish']) - pd.Timedelta(difference, unit='hours')).round(freq='s'))
				series['Duration_min'] = difference * 60
			else:
				# - If the outage duration was shorter than the average microgrid survival time, then remove the outage
				series['ComponentAff'] = None
		return series

	noncritical_mg = noncritical_mg.apply(adjust_outage_duration, axis=1)
	critical_mg = critical_mg.apply(adjust_outage_duration, axis=1)
	# - Step 6 (last step): remove outages that would have been entirely mitigated by a microgrid
	noncritical_mg = noncritical_mg[~pd.isna(noncritical_mg['ComponentAff'])]
	critical_mg = critical_mg[~pd.isna(critical_mg['ComponentAff'])]
	# - Generate resilience statistics for SAIDI and group loads by critical/noncritical and without microgrid/with microgrid
	load_count = len([x for x in tree if x.get('object','').startswith('load.')])
	df = pd.DataFrame({
		'CAIDI': [
			round(stats(noncritical_without_mg, 200, load_count)[2], 3),
			round(stats(noncritical_mg, 200, load_count)[2], 3),
			round(stats(critical_mg, 200, load_count)[2], 3),
			round(stats(critical_without_mg, 200, load_count)[2], 3)]
	}, index=['noncritical_without_mg', 'noncritical_mg', 'critical_mg', 'critical_without_mg'])
	fig = go.Figure(data=[
		go.Bar(name='Noncritical Outside Microgrid', x=df.columns.to_series(), y=df.loc['noncritical_without_mg'], hoverlabel_namelength=-1),
		go.Bar(name='Noncritical Inside Microgrid', x=df.columns.to_series(), y=df.loc['noncritical_mg'], hoverlabel_namelength=-1),
		go.Bar(name='Critical Inside Microgrid', x=df.columns.to_series(), y=df.loc['critical_mg'], hoverlabel_namelength=-1),
		go.Bar(name='Critical Outside Microgrid', x=df.columns.to_series(), y=df.loc['critical_without_mg'], hoverlabel_namelength=-1)])
	legend_spec = {'orientation':'h', 'xanchor':'left'}#, 'x':0, 'y':-0.2}
	fig.update_layout(
		title = 'CAIDI Statistics for Four Categories of Loads Across All Years',
		legend = legend_spec,
		font = dict(
			family="sans-serif",
			color="black"))
	saidi_groupings_html = fig.to_html(default_height='300px')
	# - Generate resilience statistics for SAIDI, SAIFI, CAIDI, ASAI, and MAIFI. Compare stats by year and by no microgrids vs. microgrids
	no_microgrids_df = pd.read_csv(in_csv)
	microgrids_df = pd.concat([noncritical_without_mg, noncritical_mg, critical_mg, critical_without_mg])
	# - Compare every year that has an outage in the file. The microgrid adjustment moves an outage's start later, which can move it into the next
	#   year
	years = sorted(set(pd.to_datetime(no_microgrids_df['Start']).dt.year) | set(pd.to_datetime(microgrids_df['Start']).dt.year))
	# - Interuption of less than '200' seconds considered momentary.
	no_microgrids_stats = [
		[round(x, 3) for x in stats(no_microgrids_df[(pd.to_datetime(no_microgrids_df['Start']) >= pd.Timestamp(str(year))) & (pd.to_datetime(no_microgrids_df['Start']) < pd.Timestamp(str(year + 1)))].reset_index(), 200, load_count)]
		for year in years]
	microgrids_stats = [
		[round(x, 3) for x in stats(microgrids_df[(pd.to_datetime(microgrids_df['Start']) >= pd.Timestamp(str(year))) & (pd.to_datetime(microgrids_df['Start']) < pd.Timestamp(str(year + 1)))].reset_index(), '200', str(load_count))]
		for year in years]
	df = pd.DataFrame({
		'SAIDI': [f(i) for i in range(len(years)) for f in (lambda idx: no_microgrids_stats[idx][0], lambda idx: microgrids_stats[idx][0])],
		'SAIFI': [f(i) for i in range(len(years)) for f in (lambda idx: no_microgrids_stats[idx][1], lambda idx: microgrids_stats[idx][1])],
		'CAIDI': [f(i) for i in range(len(years)) for f in (lambda idx: no_microgrids_stats[idx][2], lambda idx: microgrids_stats[idx][2])],
		'ASAI':  [f(i) for i in range(len(years)) for f in (lambda idx: no_microgrids_stats[idx][3], lambda idx: microgrids_stats[idx][3])],
		'MAIFI': [f(i) for i in range(len(years)) for f in (lambda idx: no_microgrids_stats[idx][4], lambda idx: microgrids_stats[idx][4])]
	}, index=[f'stats_{kind}_{year}' for year in years for kind in ('raw', 'new')])
	fig = go.Figure(data=[
		go.Bar(name=f'{name} ({year})', x=df.columns.to_series(), y=df.loc[f'stats_{kind}_{year}'], marker_color=color, hoverlabel_namelength=-1)
		for year in years
		for kind, name, color in (('raw', 'Without Microgrid', '#fa9563'), ('new', 'Microgrid', '#48cb11'))])
	fig.update_layout(
		title = 'Original and Adjusted Resilience Metrics by Year',
		legend = legend_spec,
		font = dict(
			family="sans-serif",
			color="black"))
	years_grouping_html = fig.to_html(default_height='300px')
	# - Generate outage table
	# - First, generate microgrid-related columns
	microgrid_meters = pd.concat([noncritical_mg, critical_mg]).drop(columns=['ComponentAff', 'Start', 'Finish', 'Microgrid', 'Criticality'])
	microgrid_meters = (microgrid_meters.groupby([microgrid_meters.index])
		.agg({'Duration_min': 'mean', 'Meters Affected': lambda x: ' '.join(x)})
		.rename(columns={'Duration_min': 'Average Microgrid Duration_min', 'Meters Affected': 'Microgrid Meters Affected'}))
	microgrid_meters['Microgrid Meter Count'] = microgrid_meters['Microgrid Meters Affected'].apply(lambda string: len(string.split()))
	microgrid_meters['Microgrid Customer Outage Minutes'] = microgrid_meters['Microgrid Meter Count'] * microgrid_meters['Average Microgrid Duration_min']
	# - Second, generate non-microgrid-related columns
	no_microgrids_df['Date'] = no_microgrids_df['Start'].apply(lambda string: str(pd.to_datetime(string).to_period(freq='d')))
	no_microgrids_df['Meter Count'] = no_microgrids_df['Meters Affected'].apply(lambda string: len(string.split()) if not pd.isna(string) else 0)
	no_microgrids_df['Customer Outage Minutes'] = no_microgrids_df.apply(lambda series: len(series['Meters Affected'].split()) * series['Duration_min'] if not pd.isna(series['Meters Affected']) else 0, axis=1)
	# - Third, join dataframes, fill in blanks row cells, and round
	table_df = no_microgrids_df.join(microgrid_meters)
	table_df['Meters Affected'] = table_df['Meters Affected'].fillna('None')
	table_df['Average Microgrid Duration_min'] = table_df['Average Microgrid Duration_min'].fillna(0)
	table_df['Microgrid Meters Affected'] = table_df['Microgrid Meters Affected'].fillna('None')
	table_df['Microgrid Meter Count'] = table_df['Microgrid Meter Count'].fillna(0)
	table_df['Microgrid Customer Outage Minutes'] = table_df['Microgrid Customer Outage Minutes'].fillna(0)
	table_df['Duration_min'] = table_df['Duration_min'].round(2)
	table_df['Average Microgrid Duration_min'] = table_df['Average Microgrid Duration_min'].round(2)
	table_df['Customer Outage Minutes'] = table_df['Customer Outage Minutes'].round(2)
	table_df['Microgrid Customer Outage Minutes'] = table_df['Microgrid Customer Outage Minutes'].round(2)
    # - Fourth, reorder columns
	table_df = table_df.reindex(columns=[
		'ComponentAff',
        'Start',
		'Finish',
		'Duration_min',
		'Meters Affected',
		'Average Microgrid Duration_min',
		'Microgrid Meters Affected',
		'Date',
		'Meter Count',
		'Microgrid Meter Count',
		'Customer Outage Minutes',
		'Microgrid Customer Outage Minutes'])
	table_html = '<h1>Full Outage History and Microgrid Adjustments</h1>' + table_df.to_html()
	# - Generate outage timelines
	fig = go.Figure(
		data=[
			go.Bar(
				name = 'Customer Outage Minutes',
				x=[str(x)[0:7] for x in table_df['Date']], # aggregate to the month level.
				y=[float(x) for x in table_df['Customer Outage Minutes']]),
			go.Bar(
				name='Microgrid Customer Outage Minutes',
				x=[str(x)[0:7] for x in table_df['Date']], # aggregate to the month level.
				y=[float(x) for x in table_df['Microgrid Customer Outage Minutes']])])
	fig.update_layout(
		title = 'Outage Timeline, Original and Microgrid',
		legend = legend_spec,
		font = dict(
			family="sans-serif",
			color="black"))
	outage_timeline_html = fig.to_html(default_height='600px')
	style_string = '<head><style>* {font-family:sans-serif}; .dataframe {width:100%; border-collapse:collapse; overflow-x:scroll}</style></head>'
	# TODO: fix table styling, transition to jinja2 template.
	with open(out_html,'w') as outFile:
		outFile.write(style_string + '\n' + saidi_groupings_html + '\n' + years_grouping_html + '\n' + outage_timeline_html + '\n' + table_html)


def _customer_outage_cost(csv_path):
	rows = [x.split(',') for x in open(csv_path).readlines()]
	header = rows[0]
	values = rows[1:]
	# print(header, values)
	for in_row in values:
		payload = in_row[1:5]
		print(in_row[0], payload)
		out = customerCost1(*payload)
		print(out)


def _utility_outage_cost_TEST():
	# utilityOutageTable(average_lost_kwh, profit_on_energy_sales, restoration_cost, hardware_cost, outageDuration, output_dir)
	utilcost = utilityOutageTable([1000,1000,1000], 0.02, 5000, 9000, 5, None) #None = tempfile output.
	print(utilcost)


def __test_getting_affloads(affected_obj='692675', test_file='lehigh_base_phased.dss'):
	''' Test getting loads affected by an outage on a TREE. '''
	tree = dssConvert.dssToTree(test_file)
	sub_obs = opendss.get_subtree_obs(affected_obj, tree)
	print('SUB_OBS', len(sub_obs), sub_obs)
	sub_loads = [x['object'][5:] for x in sub_obs if x.get('object','').startswith('load.')]
	print('SUBLOAD_NAMES', len(sub_loads), sub_loads)


def _write_csv(filename, fields, rows):
	''' Helper function to write a CSV. '''
	with open(filename, 'w') as csvfile:
		csvwriter = csv.writer(csvfile)
		csvwriter.writerow(fields)
		csvwriter.writerows(rows)


def _random_outage(tree):
	''' Generate single random outage on a tree, return affected loads. '''
	all_line_names = [x.get('object')[5:] for x in tree if x.get('object','').startswith('line.')]
	all_transformer_names = [x.get('object')[12:] for x in tree if x.get('object', '').startswith('transformer.')]
	all_line_names.extend(all_transformer_names)
	# for lehigh: all_line_names = ['670671','645646','692675','684611','684652','671692','632633','671684','632645','632670','650632','671680']
	out_line = random.choice(all_line_names)
	duration = exponential(scale=100)
	start_date = f'{randint(2015, 2021)}-{randint(1,12)}-{randint(1,28)} {randint(0,23)}:{randint(0,59)}:{randint(0,59)}'
	start_date_dt = dt.strptime(start_date, '%Y-%m-%d %H:%M:%S')
	end_date_dt = start_date_dt + td(minutes=duration)
	end_date = dt.strftime(end_date_dt, '%Y-%-m-%-d %-H:%M:%S') # yuck! stftime and strptime have different format string syntaxes
	obs_affected = opendss.get_subtree_obs(out_line, tree)
	loads_affected = [x['object'][5:] for x in obs_affected if x.get('object','').startswith('load.')]
	return [out_line, start_date, end_date, duration, ' '.join(loads_affected)]


def _many_random_outages(count, out_filename, in_dss):
	''' Create <count> outages for the Lehigh circuit. '''
	tree = dssConvert.dssToTree(in_dss)
	rows = []
	for x in range(count):
		rows.append(_random_outage(tree))
	OUTAGE_CSV_COLS = ['ComponentAff', 'Start', 'Finish', 'Duration_min', 'Meters Affected']
	_write_csv(out_filename, OUTAGE_CSV_COLS, rows)


def _tests():
	# - Test outage generation
	#_many_random_outages(100, './testfiles/lehigh_random_outages999.csv', './testfiles/lehigh_base_phased.dss')
	# - Test model run
	test_model = 'lehigh1mg'
	absolute_model_directory = f'{microgridup.PROJ_DIR}/{test_model}'
	# HACK: work in directory because we're very picky about the current dir.
	curr_dir = os.getcwd()
	if curr_dir != absolute_model_directory:
		os.chdir(absolute_model_directory)
	with open('allInputData.json') as file:
		data = json.load(file)
	main('outages.csv', data, 'circuit.dss', 'zoutput.html')
	assert os.path.isfile('zoutput.html')
	#os.system('open zoutput.html')
	os.chdir(curr_dir)
	#_utility_outage_cost_TEST()
	#_customer_outage_cost(f'{omf.omfDir}/static/testFiles/customerInfo.csv')


if __name__ == '__main__':
	_tests()