import json, multiprocessing, os, platform, shutil, datetime, time, markdown, re, traceback
from pathlib import Path
from subprocess import Popen
from collections import OrderedDict
from matplotlib import pyplot as plt
import networkx as nx
from flask_httpauth import HTTPBasicAuth
from werkzeug.security import generate_password_hash, check_password_hash
from flask import Flask, Request, request, redirect, render_template, jsonify, url_for, send_from_directory, Blueprint
from omf import geo
from omf.solvers.opendss import dssConvert
from microgridup_gen_mgs import nx_group_branch, nx_group_lukes, nx_bottom_up_branch, nx_critical_load_branch, get_all_trees, form_microgrids, form_mg_groups, topological_sort, SwitchNotFoundError, CycleDetectedError, InsufficientBranchPointsError
# from microgridup_gen_mgs import new_nx_group_branch # Add to above imports when transitioning to new_nx_group_branch().
import microgridup_validation
import microgridup

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16 MB
app.config['MAX_FORM_MEMORY_SIZE'] = 16 * 1024 * 1024  # 16 MB

'''Set error handlers.'''
@app.errorhandler(ValueError) # Helpful for unexpected Python function errors.
def handle_value_error(error):
	response = jsonify(message=str(error), error=error.__class__.__name__)
	response.status = 400
	return response

@app.errorhandler(InsufficientBranchPointsError)
def handle_insufficient_branch_points_error(error):
	response = jsonify(message=str(error), error=error.__class__.__name__)
	response.status_code = 422
	return response

@app.errorhandler(SwitchNotFoundError)
def handle_switch_not_found_error(error):
	response = jsonify(message=str(error), error=error.__class__.__name__)
	response.status_code = 422
	return response

@app.errorhandler(CycleDetectedError)
def handle_cycle_detected_error(error):
	response = jsonify(message=str(error), error=error.__class__.__name__)
	response.status_code = 422
	return response

@app.errorhandler(400)
def bad_request(error):
	response = jsonify(message=str(error), error=error.__class__.__name__)
	response.status_code = 400
	return response

@app.errorhandler(404)
def not_found(error):
    response = jsonify(message=str(error), error=error.__class__.__name__)
    response.status_code = 404
    return response

@app.errorhandler(500)
def internal_server_error(error):
	response = jsonify(message=str(error), error=error.__class__.__name__)
	response.status_code = 500
	return response

@app.errorhandler(Exception) # Should catch any unexpected errors.
def handle_unexpected_error(error):
	traceback.print_exc()
	response = jsonify(message=f'Unexpected server error of type {error.__class__.__name__}')
	response.status_code = 500
	return response

# - Use blueprints to add additional static directories
# - data_dir_blueprint is needed to access our model results from the data/ folder
data_dir_blueprint = Blueprint('data_dir_blueprint', __name__, static_folder='data')
app.register_blueprint(data_dir_blueprint)

# - doc_blueprint is is needed to serve images that are in the microgridup playbook documentation
doc_blueprint = Blueprint('doc_blueprint', __name__, static_folder='docs/microgridup-playbook/media')
@doc_blueprint.route('/doc', methods=['GET'])
def doc():
	with (Path(microgridup.MGU_DIR) / 'docs' / 'microgridup-playbook' / 'README.md').open() as f:
		readme = f.read()
	regex = re.compile(r'(?<=^## Table of Contents\n\n).*(?=\n\n^## Overview)', re.MULTILINE | re.DOTALL)
	md_with_html_toc = regex.sub('[TOC]', readme)
	md = markdown.Markdown(extensions=['toc'])
	html = ('<!DOCTYPE html><html lang="en"><head><style>body { width: 55em; margin: auto; }img { max-width: 55em; }'
		'table, td { border: 1px solid black; border-collapse: collapse; }</style><meta charset="utf-8">'
		f'<title>MicrogridUp documentation</title></head><body>{md.convert(md_with_html_toc)}</body></html>')
	return html
app.register_blueprint(doc_blueprint)

users = {} # if blank then no authentication.
users_path = f'{microgridup.MGU_DIR}/data/static/users.json'
if os.path.exists(users_path):
	user_json = json.load(open(users_path))
	users = {k:generate_password_hash(user_json[k]) for k in user_json}

auth = HTTPBasicAuth()

# authenticate every request if user json available.
@app.before_request
@auth.login_required
def before_request_func():
	pass #print("Performing Authentication")

@auth.verify_password
def verify_password(username, password):
	if users != {}:
		if username in users and check_password_hash(users.get(username), password):
			return username
	else:
		return username
	
@app.route('/get_logs/<model_name>')
def get_logs(model_name):
    # Read the logs from the corresponding log file.
    log_file = os.path.join('data/projects', model_name, 'logs.log')
    if os.path.exists(log_file):
        with open(log_file, 'r') as file:
            logs = file.readlines()
    else:
        logs = []
    return jsonify({"logs": logs})

def list_projects():
	projects = [x for x in os.listdir(microgridup.PROJ_DIR) if os.path.isdir(f'{microgridup.PROJ_DIR}/{x}')]
	project_timestamps = {}
	project_descriptions = {}
	
	for project in projects:
		input_data_path = os.path.join(microgridup.PROJ_DIR, project, 'allInputData.json')
		if os.path.exists(input_data_path):
			with open(input_data_path) as json_file:
				data = json.load(json_file)
				if 'CREATION_DATE' in data and data['CREATION_DATE']:
					timestamp = datetime.datetime.strptime(data['CREATION_DATE'], '%Y-%m-%d %H:%M:%S')
					project_timestamps[project] = timestamp
				if 'DESCRIPTION' in data and data['DESCRIPTION']:
					project_descriptions[project] = data['DESCRIPTION']
	return projects, project_timestamps, project_descriptions

@app.route('/')
def home():
	projects, project_timestamps, project_descriptions = list_projects()
	return render_template('template_home.html', projects=projects, project_timestamps=project_timestamps, project_descriptions=project_descriptions)

@app.route('/healthcheck')
def healthcheck():
    return jsonify(status="running")

@app.route('/rfile/<project>/<filename>')
def rfile(project, filename):
	return send_from_directory(os.path.join(microgridup.PROJ_DIR, project), filename)
	
@app.route('/rfile/<project>/<subfolder>/<filename>')
def rnested_file(project, subfolder, filename):
	return send_from_directory(os.path.join(microgridup.PROJ_DIR, project, subfolder), filename)

@app.route('/load/<project>')
def load(project):
	ana_files = os.listdir(f'{microgridup.PROJ_DIR}/{project}')
	input_data_path = os.path.join(microgridup.PROJ_DIR, project, 'allInputData.json')
	if os.path.exists(input_data_path):
		with open(input_data_path) as json_file:
			data = json.load(json_file)
	else:
		print('Error: Input data failed to save.')
	if '0running.txt' in ana_files:
		return render_template('template_in_progress.html', model_name=project, in_data=data, crashed=False)
	elif '0crashed.txt' in ana_files:
		return render_template('template_in_progress.html', model_name=project, in_data=data, crashed=True)
	elif 'output_final.html' in ana_files:
		return redirect(f'/data/projects/{project}/output_final.html')
	else:
		return 'Model is in an inconsistent state. Please delete and recreate.'
	
@app.route('/check_status/<project>')
def check_status(project):
	'''Used by template_in_progress.html to redirect to output_final.html once it exists. Used by template_home.html to update status in boxes.'''
	files = os.listdir(f'{microgridup.PROJ_DIR}/{project}')
	if 'output_final.html' in files and '0running.txt' not in files and '0crashed.txt' not in files:
		return jsonify(status='complete', url=f'/data/projects/{project}/output_final.html')
	elif '0crashed.txt' in files:
		return jsonify(status='crashed')
	else:
	    return jsonify(status='in_progress')

@app.route('/edit/<project>')
def edit(project):
	try:
		with open(f'{microgridup.PROJ_DIR}/{project}/allInputData.json') as in_data_file:
			in_data = json.load(in_data_file)
	except:
		in_data = None
	# - Encode the circuit model properly
	if 'jsCircuitModel' in in_data:
		jsCircuitModel = []
		for s in json.loads(in_data['jsCircuitModel']):
			jsCircuitModel.append(json.loads(s))
		in_data['jsCircuitModel'] = jsCircuitModel
	# - TODO: can we remove this if we update our committed "data/projects" directory? Probably not because existing user projects will break
	# Inject defaults for any REOPT_INPUTS keys added after a project was created (backwards compatibility).
	reopt_defaults = {'batterySocMinFraction': 0.2, 'urdbResponse': ''}
	for key, default in reopt_defaults.items():
		if key not in in_data.get('REOPT_INPUTS', {}):
			in_data.setdefault('REOPT_INPUTS', {})[key] = default
	return render_template('template_new.html', in_data=in_data, iframe_mode=False, editing=True)

@app.route('/delete/<project>')
def delete(project):
	full_path = f'{microgridup.PROJ_DIR}/{project}'
	if os.path.exists(full_path) and os.path.isdir(full_path):
		shutil.rmtree(full_path)
	else:
		return 'Directory does not exist or is not a directory.'
	return redirect(url_for('home'))

@app.route('/new')
def new_gui():
	with open(f'{microgridup.MGU_DIR}/static/lehigh_3mg_inputs.json') as default_in_file:
		default_in = json.load(default_in_file)
	return render_template('template_new.html', in_data=default_in, iframe_mode=False, editing=False)

@app.route('/duplicate', methods=["POST"])
def duplicate():
	model_name = request.json.get('project', None)
	new_name = request.json.get('new_name', None)
	projects, project_timestamps, project_descriptions = list_projects()
	if (model_name not in projects) or (new_name in projects):
		return 'Duplication failed. Project does not exist or the new name is invalid.'
	else:
		shutil.copytree(f'{microgridup.PROJ_DIR}/{model_name}', f'{microgridup.PROJ_DIR}/{new_name}')
		with open(f'{microgridup.PROJ_DIR}/{new_name}/allInputData.json') as file:
			inputs = json.load(file)
		inputs['MODEL_DIR'] = inputs['MODEL_DIR'].replace(model_name, new_name)
		inputs['BASE_DSS'] = inputs['BASE_DSS'].replace(model_name, new_name)
		inputs['LOAD_CSV'] = inputs['LOAD_CSV'].replace(model_name, new_name)
		if inputs.get('OUTAGE_CSV'):
			inputs['OUTAGE_CSV'] = inputs['OUTAGE_CSV'].replace(model_name, new_name)
		with open(f'{microgridup.PROJ_DIR}/{new_name}/allInputData.json', 'w') as file:
			json.dump(inputs, file, indent=4)
		# - Update output_final.html
		with open(f'{microgridup.PROJ_DIR}/{new_name}/output_final.html') as file:
			html_content = file.read()
		patterns = [
			(r'<title>MicrogridUP &raquo; ' + model_name + '</title>', r'<title>MicrogridUP &raquo; ' + new_name + r'</title>'),
			(r'<span class="span--sectionTitle">MicrogridUp &raquo; '+ model_name +' &raquo;</span>', r'<span class="span--sectionTitle">MicrogridUp &raquo; ' + new_name + r' &raquo;</span>'),
			(r'<a href="/edit/' + model_name + '"', r'<a href="/edit/' + new_name + '"', )
		]
		for pattern, repl in patterns:
			html_content = re.sub(pattern, repl, html_content)
		ul_pattern = re.compile(r'(<ul[^>]*>.*?<\/ul>)', re.DOTALL)

		def replace_in_ul(match):
			ul_content = match.group(1)
			ul_content = re.sub(r'(/rfile/' + re.escape(model_name) + r'/)', r'/rfile/' + new_name + r'/', ul_content)
			return ul_content

		html_content = ul_pattern.sub(replace_in_ul, html_content)
		with open(f'{microgridup.PROJ_DIR}/{new_name}/output_final.html', 'w', encoding='utf-8') as file:
			file.write(html_content)
		# - Update view_inputs.html
		with open(f'{microgridup.PROJ_DIR}/{new_name}/view_inputs.html') as file:
			html_content = file.read()
		patterns = [
			(f'"{model_name}"', f'"{new_name}"'),
			(f'/{model_name}/', f'/{new_name}/'),
		]
		for pattern, repl in patterns:
			html_content = re.sub(pattern, repl, html_content)
		with open(f'{microgridup.PROJ_DIR}/{new_name}/view_inputs.html', 'w', encoding='utf-8') as file:
			file.write(html_content)
		return jsonify(f'Successfully duplicated {model_name} as {new_name}.')

@app.route('/wizard_to_dss', methods=['GET','POST'])
def wizard_to_dss(model_dir=None, lat=None, lon=None, elements=None, test_run=False, on_edit_flow=None):
	if not model_dir:
		model_dir = request.form['MODEL_DIR']
	if not lat:
		lat = float(request.form['latitude'])
	if not lon:
		lon = float(request.form['longitude'])
	if not elements:
		elements = json.loads(request.form['json'])
	if not on_edit_flow:
		on_edit_flow = request.form['onEditFlow']
	if on_edit_flow == 'false':
		if os.path.isdir(f'{microgridup.PROJ_DIR}/{model_dir}'):
			print('Invalid Model Name.')
			return jsonify(error=f'A project named "{model_dir}" already exists. Please choose a different project name.'), 400 # Name was already taken.
	# Convert to DSS and return loads.
	dssString = f'clear \nset defaultbasefrequency=60 \nnew object=circuit.{model_dir} \n'
	busList = []
	# Name substation bus after substation itself. Name gen bus after the feeder.
	for ob in elements:
		obType = ob['type']
		obName = ob['name']
		suffix = '' if obName.endswith('_existing') else '_existing'
		if obType == 'substation': 
			dssString += f'new object=vsource.{obName} basekv={ob["basekv"]} bus1={obName}_bus.1.2.3 pu=1.00 r1=0 x1=0.0001 r0=0 x0=0.0001 \n'
			busList.append(f'{obName}_bus')
		elif obType == 'feeder':
			dssString += f'new object=line.{obName} phases=3 bus1={ob["parent"]}_bus.1.2.3 bus2={obName}_end.1.2.3 length=1333 units=ft \n'
			busList.append(f'{obName}_end')
		elif obType == 'load':
			dssString += f'new object=load.{obName} bus1={ob["parent"]}_end.1.2.3 phases=3 conn=wye model=1 kv=2.4 kw={ob["kw"]} kvar=660 \n'
		elif obType == 'solar':
			prefix = '' if obName.startswith('solar_') else 'solar_'
			dssString += f'new object=generator.{prefix}{obName}{suffix} bus1={ob["parent"]}_end.1.2.3 phases=3 kv=2.4 kw={ob["kw"]} pf=1 \n'
		elif obType == 'wind':
			prefix = '' if obName.startswith('wind_') else 'wind_'
			dssString += f'new object=generator.{prefix}{obName}{suffix} bus1={ob["parent"]}_end.1.2.3 phases=3 kv=2.4 kw={ob["kw"]} pf=1 \n'
		elif obType == 'battery':
			prefix = '' if obName.startswith('battery_') else 'battery_'
			dssString += f'new object=storage.{prefix}{obName}{suffix} bus1={ob["parent"]}_end.1.2.3 phases=3 kv=2.4 kwrated={ob["kw"]} kwhstored={ob["kwh"]} kwhrated={ob["kwh"]} dispmode=follow %charge=100 %discharge=100 %effcharge=96 %effdischarge=96 \n'
		elif obType == 'fossil':
			prefix = '' if obName.startswith('fossil_') else 'fossil_'
			dssString += f'new object=generator.{prefix}{obName}{suffix} bus1={ob["parent"]}_end.1.2.3 phases=3 kw={ob["kw"]} pf=1 kv=2.4 xdp=0.27 xdpp=0.2 h=2 \n'
		else:
			raise Exception(f'Unknown object type "{obType}"')
	# Convert dssString to a networkx graph.
	tree = dssConvert.dssToTree(dssString, is_path=False)
	G = dssConvert.dss_to_networkx('', tree=tree)
	# Set twopi layout to custom coordinates.
	pos = nice_pos(G)
	# Define the scale
	scale_factor = 0.00005
	# Calculate the translation coordinates
	x_offset = lon - (scale_factor * (max(pos.values(), key=lambda x: x[0])[0] - min(pos.values(), key=lambda x: x[0])[0])) / 2
	y_offset = lat - (scale_factor * (max(pos.values(), key=lambda x: x[1])[1] - min(pos.values(), key=lambda x: x[1])[1])) / 2
	# Apply the translation and scaling to the layout
	new_pos = {node: (scale_factor * (x - min(pos.values(), key=lambda x: x[0])[0]) + x_offset, scale_factor * (y - min(pos.values(), key=lambda x: x[1])[1]) + y_offset) for node, (x, y) in pos.items()}
	dssString += 'makebuslist \n'
	for bus in busList:
		new_pos_bus = bus.lower()
		dssString += f'setbusxy bus={bus} y={new_pos[new_pos_bus][1]} x={new_pos[new_pos_bus][0]} \n'
	dssString += 'set voltagebases=[115,4.16,0.48]\ncalcvoltagebases'
	if not os.path.isdir(f'{microgridup.MGU_DIR}/uploads'):
		os.mkdir(f'{microgridup.MGU_DIR}/uploads')
	dssFilePath = f'{microgridup.MGU_DIR}/uploads/BASE_DSS_{model_dir}'
	with open(dssFilePath, "w") as outFile:
		outFile.writelines(dssString)
	loads = getLoads(dssFilePath)
	if not test_run:
		t = dssConvert.dssToTree(dssFilePath)
		return jsonify(loads=loads, filename=dssFilePath)
	else:
		print(f'Test run of wizard_to_dss() for {model_dir} complete.')
		return dssFilePath

@app.route('/uploadDss', methods = ['POST'])
def uploadDss():
	model_dir = request.form['MODEL_DIR']
	# Check to see if user is on edit flow. If user is not on edit flow, ensure that model_dir is not already in data/projects.
	on_edit_flow = request.form['onEditFlow']
	if on_edit_flow == 'false':
		if os.path.isdir(f'{microgridup.PROJ_DIR}/{model_dir}'):
			raise ValueError(f'A model named "{model_dir}" already exists. Please choose a different Model Name.')
	# Check if the post request has the file part.
	if 'BASE_DSS_NAME' not in request.files:
		raise ValueError('No file part.')
	file = request.files['BASE_DSS_NAME']
	# If the user does not select a file, the browser submits an empty file without a filename.
	if file.filename == '':
		raise ValueError('No selected file.')
	if file and allowed_file(file.filename):
		if not os.path.isdir(f'{microgridup.MGU_DIR}/uploads'):
			os.mkdir(f'{microgridup.MGU_DIR}/uploads')
		file.save(f'{microgridup.MGU_DIR}/uploads/BASE_DSS_{model_dir}')
		loads = getLoads(f'{microgridup.MGU_DIR}/uploads/BASE_DSS_{model_dir}')
		return jsonify(loads=loads, filename=f'{microgridup.MGU_DIR}/uploads/BASE_DSS_{model_dir}')
	raise ValueError('Invalid file.')

@app.route('/getLoadsFromExistingFile', methods=['POST'])
def getLoadsFromExistingFile():
	request_json = request.get_json()
	model_dir = request_json['MODEL_DIR']
	path = f'{microgridup.PROJ_DIR}/{model_dir}/circuit.dss'
	loads = getLoads(path)
	return jsonify(loads=loads)

@app.route('/previewOldPartitions', methods=['POST'])
def previewOldPartitions():
	request_json = request.get_json()
	model_dir = request_json['MODEL_DIR']
	filename = f'{microgridup.PROJ_DIR}/{model_dir}/circuit.dss'
	MICROGRIDS = request_json['MICROGRIDS']
	CRITICAL_LOADS = request_json['CRITICAL_LOADS']
	# Convert to omd to give coordinates to important grid items. Save in static directory.
	output_dir = os.path.join('static', 'preview_partition_maps', model_dir)
	os.makedirs(output_dir, exist_ok=True)
	omd_path = os.path.join(output_dir, 'preview_old_partitions_omd.omd')
	omd = dssConvert.dssToOmd(filename, omd_path, RADIUS=0.0004, write_out=True)
	G = dssConvert.dss_to_networkx(filename, omd=omd)
	parts = []
	for mg in MICROGRIDS:
		cur_mg = []
		cur_mg.append(MICROGRIDS[mg].get('gen_bus'))
		try:
			# Try to extend mg by all elements topographically downstream from gen_bus.
			all_descendants = list(nx.descendants(G, MICROGRIDS[mg].get('gen_bus')))
			cur_mg.extend(all_descendants)
		except:
			# If that didn't work, use old method.
			cur_mg.extend([load for load in MICROGRIDS[mg].get('gen_obs_existing')])
			cur_mg.extend([load for load in MICROGRIDS[mg].get('loads')])
		parts.append(cur_mg)
	# Check to see if omd contains coordinates for each important node.
	if has_full_coords(omd):
		pos = build_pos_from_omd(omd)
		# If dss had coords, dssToOmd gave all important elements coords. Can remove all elements without coords.
		remove_nodes_without_coords(G, omd)
	else:
		pos = nice_pos(G)
		# Remove elements like 'clear' and 'set' which have no edges to other nodes.
		remove_nodes_without_edges(G)
	# Embed the map. 
	microgrids_as_mapping_proxy_type = microgridup.get_immutable_dict(MICROGRIDS)
	critical_loads_as_tuple = tuple(CRITICAL_LOADS)
	out = microgridup.colorby_mgs(omd_path, microgrids_as_mapping_proxy_type, critical_loads_as_tuple)
	# Need to reopen omd from file since saved file is different from return variable.
	omd_from_file = json.load(open(omd_path))
	omd_from_file['attachments'] = out
	with open(omd_path, 'w+') as out_file:
		json.dump(omd_from_file, out_file, indent=4)
	geo.map_omd(omd_path, output_dir, open_browser=False, showAddNewObjectsButton=False, showAttachmentsButton=False, showAddGeojsonButton=False)
	map_html_file = os.path.join(output_dir, 'geoJson_offline.html')
	map_url = url_for('static', filename=f'preview_partition_maps/{model_dir}/{os.path.basename(map_html_file)}')
	return jsonify({'map_url': map_url, 'MICROGRIDS': MICROGRIDS})

def build_pos_from_omd(omd):
	'''
	pos = {n: (n, n) for n in G}
	return type: {obj_name: (x, y), etc.}
	'''
	pos = {}
	for key in omd:
		ob = omd[key]
		if 'latitude' in ob and 'longitude' in ob:
			name = ob.get('name')
			lat = float(ob.get('latitude'))
			lon = float(ob.get('longitude'))
			pos[name] = (lon,lat)
	return pos

def has_full_coords(omd):
	has_coords = True
	should_have_coords = set(['vsource','load','generator','storage','capacitor','bus'])
	for key in omd:
		ob = omd[key]
		if ob.get('object') in should_have_coords:
			if not (ob.get('latitude') and ob.get('longitude')):
				has_coords = False
	return has_coords

def remove_nodes_without_coords(G, omd):
	all_nodes_with_coords = set()
	for key in omd:
		ob = omd[key]
		if 'latitude' in ob and 'longitude' in ob:
			all_nodes_with_coords.add(ob.get('name'))
	for node in list(G.nodes()):
		if node not in all_nodes_with_coords:
			G.remove_node(node)

def remove_nodes_without_edges(G):
	isolated_nodes = [node for node, degree in G.degree() if degree == 0]
	G.remove_nodes_from(isolated_nodes)

@app.route('/previewPartitions', methods = ['GET','POST'])
@app.route('/edit/previewPartitions', methods = ['GET','POST'])
def previewPartitions():
	request_json = request.get_json()
	MODEL_DIR = request_json['modelDir']
	CIRC_FILE = request_json['fileName']
	if not CIRC_FILE.startswith('/'):
		CIRC_FILE = str(Path(microgridup.PROJ_DIR).resolve(True) / request_json['modelDir'] / CIRC_FILE)
	CRITICAL_LOADS = request_json['critLoads']
	METHOD = request_json['method']
	MGQUANT = int(request_json['mgQuantity'])
	# Convert to omd to give coordinates to important grid items. Save in static directory.
	output_dir = os.path.join('static', 'preview_partition_maps', MODEL_DIR)
	os.makedirs(output_dir, exist_ok=True)
	omd_path = os.path.join(output_dir, 'preview_partitions_omd.omd')
	omd = dssConvert.dssToOmd(CIRC_FILE, omd_path, RADIUS=0.0004, write_out=True)
	# Convert omd to NetworkX graph because omd has full coordinates.
	G = dssConvert.dss_to_networkx(CIRC_FILE, omd=omd)
	# Check to see if omd contains coordinates for each important node.
	if has_full_coords(omd):
		pos = build_pos_from_omd(omd)
		# If dss had coords, dssToOmd gave all important elements coords. Can remove all elements without coords.
		remove_nodes_without_coords(G, omd)
	else:
		pos = nice_pos(G)
		# Remove elements like 'clear' and 'set' which have no edges to other nodes.
		remove_nodes_without_edges(G)
	algo_params={}
	all_trees = get_all_trees(G)
	all_trees_pruned = [tree for tree in all_trees if len(tree.nodes()) > 1]
	num_trees_pruned = len(all_trees_pruned)
	MG_GROUPS = []
	try:
		for tree in all_trees_pruned:
			if METHOD == 'lukes':
				default_size = int(len(tree.nodes())/3)
				MG_GROUPS.extend(nx_group_lukes(tree, algo_params.get('size',default_size)))
			elif METHOD == 'branch':
				MG_GROUPS.extend(nx_group_branch(tree, i_branch=algo_params.get('i_branch',0), omd=omd))
				# MG_GROUPS.extend(new_nx_group_branch(tree, MGQUANT, omd=omd)) # Uncomment when transitioning to new_nx_group_branch().
			elif METHOD == 'bottomUp':
				MG_GROUPS.extend(nx_bottom_up_branch(tree, num_mgs=MGQUANT/num_trees_pruned, large_or_small='large', omd=omd, cannot_be_mg=['regcontrol']))
			elif METHOD == 'criticalLoads':
				MG_GROUPS.extend(nx_critical_load_branch(tree, CRITICAL_LOADS, num_mgs=MGQUANT/num_trees_pruned, large_or_small='large'))
			else:
				print('Invalid algorithm. algo must be "branch", "lukes", "bottomUp", or "criticalLoads". No mgs generated.')
				return {}
	except InsufficientBranchPointsError as error:
		raise error
	except Exception:
		return jsonify('Invalid partitioning method')
	MICROGRIDS = form_microgrids(G, MG_GROUPS, omd)
	microgridup_validation.raise_for_invalid_microgrids(MICROGRIDS, omd, METHOD)
	# - The warnings are shown under the map
	warnings = (microgridup_validation.get_outside_load_warnings(MICROGRIDS, omd)
		+ microgridup_validation.get_no_critical_load_warnings(MICROGRIDS, CRITICAL_LOADS)
		+ microgridup_validation.get_microgrid_count_warnings(MICROGRIDS, METHOD, MGQUANT))
	# Embed the map. 
	microgrids_as_mapping_proxy_type = microgridup.get_immutable_dict(MICROGRIDS)
	critical_loads_as_tuple = tuple(CRITICAL_LOADS)
	out = microgridup.colorby_mgs(omd_path, microgrids_as_mapping_proxy_type, critical_loads_as_tuple)
	# Need to reopen omd from file since saved file is different from return variable.
	omd_from_file = json.load(open(omd_path))
	omd_from_file['attachments'] = out
	with open(omd_path, 'w+') as out_file:
		json.dump(omd_from_file, out_file, indent=4)
	geo.map_omd(omd_path, output_dir, open_browser=False, showAddNewObjectsButton=False, showAttachmentsButton=False, showAddGeojsonButton=False)
	map_html_file = os.path.join(output_dir, 'geoJson_offline.html')
	map_url = url_for('static', filename=f'preview_partition_maps/{MODEL_DIR}/{os.path.basename(map_html_file)}')
	return jsonify({'map_url': map_url, 'MICROGRIDS': MICROGRIDS, 'warnings': warnings})

@app.route('/has_cycles', methods=['GET','POST'])
def has_cycles():
	model_dir = request.json['MODEL_DIR']
	dss_path_indicator = request.json['DSS_PATH_INDICATOR']
	if dss_path_indicator == 'DIRECT TO UPLOADS FOLDER':
		dss_path = f'{microgridup.MGU_DIR}/uploads/BASE_DSS_{model_dir}' # New circuit uploaded/created.
	elif dss_path_indicator == 'circuit.dss':
		dss_path = f'{microgridup.PROJ_DIR}/{model_dir}/circuit.dss' # Reusing circuit.dss in project directory.
	else:
		print(f'Unexpected dss_path_indicator: {dss_path_indicator}.')
	G = dssConvert.dss_to_networkx(dss_path)
	try:
		list(topological_sort(G))
		return jsonify(result=False)
	except CycleDetectedError as error:
		raise error

@app.route('/run', methods=["POST"])
def run():
	# - Make the uploads directory if it doesn't already exist.
	if not os.path.isdir(f'{microgridup.MGU_DIR}/uploads'):
		os.mkdir(f'{microgridup.MGU_DIR}/uploads')
	absolute_model_directory = f'{microgridup.PROJ_DIR}/{request.form["MODEL_DIR"]}'
	model_name = request.form['MODEL_DIR']
	print(f'-------------------------------Running {model_name}.-------------------------------')
	data = {**request.form}
	# - Save uploaded files and get paths to files
	data['OUTAGE_CSV'] = _get_uploaded_file_filepath(absolute_model_directory, 'outages.csv', f'{microgridup.MGU_DIR}/uploads/HISTORICAL_OUTAGES_{model_name}', request, 'HISTORICAL_OUTAGES', 'OUTAGES_PATH')
	data['LOAD_CSV'] = _get_uploaded_file_filepath(absolute_model_directory, 'loads.csv', f'{microgridup.MGU_DIR}/uploads/LOAD_CSV_{model_name}', request, 'LOAD_CSV', 'LOAD_CSV_NAME')
	data['BASE_DSS'] = _get_uploaded_file_filepath(absolute_model_directory, 'circuit.dss', f'{microgridup.MGU_DIR}/uploads/BASE_DSS_{model_name}', request, None, 'DSS_PATH')
	data['ADDITIONAL_LOADSHAPE_CSV'] = _get_uploaded_file_filepath(absolute_model_directory, 'additional_loadshape.csv', f'{microgridup.MGU_DIR}/uploads/ADDITIONAL_LOADSHAPE_{model_name}', request, 'ADDITIONAL_LOADSHAPE', 'ADDITIONAL_LOADSHAPE_NAME')
	data['ADDITIONAL_LOADSHAPE_METER'] = request.form.get('ADDITIONAL_LOADSHAPE_METER', '')  # Meter name from form
	# Clean up like LOAD_CSV
	if 'ADDITIONAL_LOADSHAPE_NAME' in data:
		del data['ADDITIONAL_LOADSHAPE_NAME']
	# - Delete form keys that are not currently used past this point
	del data['DSS_PATH']
	del data['OUTAGES_PATH']
	if 'LOAD_CSV_NAME' in data:
		del data['LOAD_CSV_NAME']
	if 'HISTORICAL_OUTAGES' in data:
		del data['HISTORICAL_OUTAGES']
	# - Format the REopt inputs into the schema we want. This formatting needs to be done here (and not in microgridup.main) because otherwise
	#   invocations of microgridup.main() would require REopt keys to be at the top level of the user's input dict, which would be really annoying
	data['REOPT_INPUTS'] = _get_reopt_inputs(data)
	data['LOAD_GROWTH_PERCENT'] = float(request.form.get('LOAD_GROWTH_PERCENT', 0.0))  # Default to 0% (no growth)
	data['LOAD_GROWTH_SPECIFIC'] = json.loads(request.form.get('LOAD_GROWTH_SPECIFIC') or '{}') # Default to empty dict
	# - Format relevant properties for _get_microgrids()
	data['CRITICAL_LOADS'] = json.loads(data['CRITICAL_LOADS'])
	if len(data['CRITICAL_LOADS']) == 0:
		# - I'm assuming that if this is true, then the front-end allowed bad data to be sent, so we should inform the user
		return jsonify(message='No critical loads were specified. The model run was aborted.'), 400
	data['mgQuantity'] = int(data['mgQuantity'])
	# - Format faulted lines
	data['FAULTED_LINES'] = data['FAULTED_LINES'].split(',')
	# - Create microgrids here and not in microgridup.main because it's easier to format the testing data
	data['MICROGRIDS'] = _get_microgrids(data['CRITICAL_LOADS'], data['MG_DEF_METHOD'], data['mgQuantity'], data['BASE_DSS'], data['PARTITION_PARAMS'], is_wizard_circuit='jsCircuitModel' in data)
	if len(list(data['MICROGRIDS'].keys())) == 0:
		# - I'm assuming that if this is true, then the front-end allowed bad data to be sent, so we should inform the user
		raise ValueError('No microgrids were defined. The model run was aborted.')
	# - Each microgrid needs to store knowledge of parameter overrides to support auto-filling the parameter override wigdet during an edit of a model
	data['mgParameterOverrides'] = json.loads(data['mgParameterOverrides'])
	for mg_name, mg_parameter_overrides in data['mgParameterOverrides'].items():
		data['MICROGRIDS'][mg_name]['parameter_overrides'] = mg_parameter_overrides
	# - Check the rest of the inputs now, so a problem shows as an alert on the form instead of a crashed project
	microgridup_validation.raise_for_invalid_run_inputs(data)
	# - Remove form keys that are not needed past this point
	del data['MG_DEF_METHOD']
	del data['mgQuantity']
	del data['mgParameterOverrides']
	del data['PARTITION_PARAMS']
	# Kickoff the run
	new_proc = multiprocessing.Process(target=microgridup.main, args=(data,))
	new_proc.start()
	# Redirect to home after waiting a little for the file creation to happen.
	time.sleep(5)
	return redirect(f'/load/{model_name}')

def _get_uploaded_file_filepath(absolute_model_directory, filename, save_path, request, files_key, form_key):
	'''
    Save the uploaded file and get the path to the file. The file upload workflow should be the same for the load CSV, the outages CSV, and the DSS
	file, but the DSS workflow is actually different. FileStorage objects cannot be passed as arguments to a multiprocessing Process object because
	FileStorage objects cannot be copied. Therefore, all work with FileStorage objects must occur before the Process is created. This function is
	complicated because our front-end file-handling logic is also complicated

	:param absolute_model_directory: the directory of the model
	:type absolute_model_directory: str
	:param filename: the filename used for this file
		E.g. outage CSVs are always named "outages.csv"
	:type filename: str
	:param save_path: the path to save the uploaded file if there was one
	:type save_path: str
	:param request: the Flask request object
	:type request: Request
	:param files_key: the key to use to look up the uploaded file in request.files
	:type files_key: str
	:return: the path to the uploaded file, or the path to the previously existing file that the user wants to use, or None
	:rtype: str or None
	'''
	assert isinstance(absolute_model_directory, str)
	assert isinstance(filename, str)
	assert isinstance(save_path, str)
	assert isinstance(request, Request)
	assert isinstance(files_key, str) or files_key is None
	assert isinstance(form_key, str)
	if request.files.get(files_key) is not None:
		if request.files[files_key].filename == '':
			# - No file was uploaded at all AND either 1) there was no previously uploaded file or 2) the user does not want to use their previously
			#   uploaded file
			# - E.g. request.files['HISTORICAL_OUTAGES'] = <FileStorage>
			# - E.g. request.form['OUTAGES_PATH'] = 'Check files'
			print(f'No "{filename}" uploaded.')
			return None
		else:
			# - A new file was uploaded, so save it
			# - E.g. request.files['HISTORICAL_OUTAGES'] == <FileStorage>
			# - E.g. request.form['OUTAGES_PATH'] = 'Check files'
			request.files[files_key].save(save_path)
			print(f'New "{filename}" uploaded.')
			return save_path
	else:
		# - For some reason, DSS file uploads do not follow the same workflow as load CSVs or outage CSVs. If they did, then this entire inner
		#   if-statement could be deleted
		if form_key == 'DSS_PATH':
			if request.form[form_key] == 'Direct to uploads folder.':
				# - Either a circuit wizard circuit was already coverted into a DSS file and saved in /uploads or a DSS file was already saved to
				#   /uploads
				# - Note that DURING an EDIT run if the user "removes" their existing circuit.dss file by clicking "Remove File", this logic will just
				#   reuse the existing file that was presumably already uploaded to /uploads
				print(f'New "{filename}" uploaded.')
				return save_path
			else:
				print(f'Reusing "{filename}" in project directory.')
				return f'{absolute_model_directory}/{filename}'
		else:
			# - No file was uploaded at all and there WAS a previously uploaded file
			# - E.g. request.files['HISTORICAL_OUTAGES'] == Nothing
			# - E.g. request.form['OUTAGES_PATH'] = 'outages.csv'
			print(f'Reusing "{filename}" in project directory.')
			return f'{absolute_model_directory}/{filename}'

def _get_reopt_inputs(data):
	'''
	- Move the key-value pairs from the data dict into a new dict and return the new dict. Also cast parameters to correct types for error checking. Also
	  delete the key-value pairs from the data dict. The net effect is that all of the key-value pairs are moved out of the top level of data and are moved
	  under the "REOPT_INPUTS" key in data

	:param data: the dict of data
	:type data: dict
	:rtype: dict
	'''
	assert isinstance(data, dict)
	reopt_inputs = {
		'energyCost':                   float(data['energyCost']),
		'wholesaleCost':                float(data['wholesaleCost']),
		'demandCost':                   float(data['demandCost']),
		'solarCanCurtail':              data['solarCanCurtail'] == 'true',
		'solarCanExport':               data['solarCanExport'] == 'true',
		# (removed) 'urdbLabelSwitch':              data['urdbLabelSwitch'] == 'true',
		# (removed) 'urdbLabel':                    data['urdbLabel'],
		'urdbResponse':                 data['urdbResponse'].strip(),
		'year':                         int(data['year']),
		'analysisYears':                int(data['analysisYears']),
		'outageDuration':               int(data['outageDuration']),
		'value_of_lost_load':           float(data['value_of_lost_load']),
		'omCostEscalator':              float(data['omCostEscalator']),
		'discountRate':                 float(data['discountRate']),
		'solar':                        data['solar'] == 'true',
		'battery':                      data['battery'] == 'true',
		'fossil':                       data['fossil'] == 'true',
		'wind':                         data['wind'] == 'true',
		'solarCost':                    float(data['solarCost']),
		'solarMax':                     float(data['solarMax']),
		'solarMin':                     float(data['solarMin']),
		'solarMacrsOptionYears':        int(data['solarMacrsOptionYears']),
		'solarItcPercent':              float(data['solarItcPercent']),
		'batteryCapacityCost':          float(data['batteryCapacityCost']),
		'batteryCapacityMax':           float(data['batteryCapacityMax']),
		'batterySocMinFraction':        float(data.get('batterySocMinFraction', 0.2)),
		'batteryCapacityMin':           float(data['batteryCapacityMin']),
		'batteryPowerCost':             float(data['batteryPowerCost']),
		'batteryPowerMax':              float(data['batteryPowerMax']),
		'batteryPowerMin':              float(data['batteryPowerMin']),
		'batteryMacrsOptionYears':      int(data['batteryMacrsOptionYears']),
		'batteryItcPercent':            float(data['batteryItcPercent']),
		'batteryPowerCostReplace':      float(data['batteryPowerCostReplace']),
		'batteryCapacityCostReplace':   float(data['batteryCapacityCostReplace']),
		'batteryPowerReplaceYear':      int(data['batteryPowerReplaceYear']),
		'batteryCapacityReplaceYear':   int(data['batteryCapacityReplaceYear']),
		'dieselGenCost':                float(data['dieselGenCost']),
		'dieselMax':                    float(data['dieselMax']),
		'dieselMin':                    float(data['dieselMin']),
		'fuelAvailable':                float(data['fuelAvailable']),
		'minGenLoading':                float(data['minGenLoading']),
		'dieselFuelCostGal':            float(data['dieselFuelCostGal']),
		'dieselCO2Factor':              float(data['dieselCO2Factor']),
		'dieselOMCostKw':               float(data['dieselOMCostKw']),
		'dieselOMCostKwh':              float(data['dieselOMCostKwh']),
		'dieselOnlyRunsDuringOutage':   data['dieselOnlyRunsDuringOutage'] == 'true',
		'dieselMacrsOptionYears':       int(data['dieselMacrsOptionYears']),
		'windCost':                     float(data['windCost']),
		'windMax':                      float(data['windMax']),
		'windMin':                      float(data['windMin']),
		'windMacrsOptionYears':         int(data['windMacrsOptionYears']),
		'windItcPercent':               float(data['windItcPercent']),
		'maxRuntimeSeconds':            int(data['maxRuntimeSeconds'])}
	for k in reopt_inputs:
		del data[k]
	return reopt_inputs

def _get_microgrids(critical_loads, partition_method, quantity, dss_path, partition_params_json, is_wizard_circuit=False):
	'''
	:param critical_loads: a list of critical loads
	:type critical_loads: list
	:param partition_method: the partition method to use to create the microgrids
	:type partition_method: str
	:param quantity: the number of microgrids to create
	:type quantity: int
	:param dss_path: the path to the DSS file
	:type dss_path: str
	:param partition_params_json: a str of optional parameters
	:type partition_params_json: str
	:return: a dict of microgrids
	:rtype: dict
	'''
	assert isinstance(critical_loads, list)
	assert isinstance(partition_method, str)
	assert isinstance(quantity, int)
	assert isinstance(dss_path, str)
	assert isinstance(partition_params_json, str)
	G = dssConvert.dss_to_networkx(dss_path)
	omd = dssConvert.dssToOmd(dss_path, '', RADIUS=0.0004, write_out=False)
	if partition_method == 'lukes':
		mg_groups = form_mg_groups(G, critical_loads, 'lukes', json.loads(partition_params_json))
		microgrids = form_microgrids(G, mg_groups, omd)
	elif partition_method == 'branch':
		mg_groups = form_mg_groups(G, critical_loads, 'branch', algo_params={'omd':omd})
		microgrids = form_microgrids(G, mg_groups, omd)
	elif partition_method == 'bottomUp':
		mg_groups = form_mg_groups(G, critical_loads, 'bottomUp', algo_params={'num_mgs':quantity, 'omd':omd, 'cannot_be_mg':['regcontrol']})
		microgrids = form_microgrids(G, mg_groups, omd)
	elif partition_method == 'criticalLoads':
		mg_groups = form_mg_groups(G, critical_loads, 'criticalLoads', algo_params={'num_mgs':quantity})
		microgrids = form_microgrids(G, mg_groups, omd)
	elif partition_method in ('loadGrouping', 'manual'):
		# - The user chose these groups, so check them before the microgrids are formed. Only a wizard circuit puts each microgrid on one feeder
		algo_params = json.loads(partition_params_json)
		mg_groups = form_mg_groups(G, critical_loads, partition_method, algo_params)
		microgridup_validation.raise_for_empty_mg_groups(mg_groups, partition_params_json)
		microgridup_validation.raise_for_mg_group_nodes_not_in_circuit(mg_groups, G, partition_params_json)
		microgridup_validation.raise_for_mg_groups_not_in_one_tree(mg_groups, G, partition_params_json)
		if is_wizard_circuit:
			microgridup_validation.raise_for_mg_groups_not_on_one_feeder(mg_groups, omd, partition_params_json)
		microgrids = form_microgrids(G, mg_groups, omd, switch_dict=algo_params.get('switch', None), gen_bus_dict=algo_params.get('gen_bus', None), mg_name_dict=algo_params.get('mg_name', None))
	elif partition_method == '':
		microgrids = json.loads(partition_params_json)
	microgridup_validation.raise_for_invalid_microgrids(microgrids, omd, partition_method)
	# Validate that intended microgrids (from PARTITION_PARAMS.mg_name) actually exist in data['MICROGRIDS']
	microgridup_validation.raise_for_missing_intended_microgrids(microgrids, partition_params_json)
	return microgrids

def allowed_file(filename):
	ALLOWED_EXTENSIONS = {'dss'}
	return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def gen_col_map(item_ob, map_dict, default_col):
	'''generate a color map for nodes or edges'''
	return [map_dict.get(n, default_col) for n in item_ob]

def node_group_map(graph, parts, color_list=['red','orange','yellow','green','blue','indigo','violet']):
	''' color map for notes in a group of [parts]. '''
	color_map = {}
	for i, group in enumerate(parts):
		for item in group:
			color_map[item] = color_list[i % len(color_list)]
	n_color_map = gen_col_map(graph.nodes(), color_map, 'gray')
	return n_color_map

def nice_pos(G):
	''' return nice positions for charting G. '''
	# return nx.drawing.nx_agraph.graphviz_layout(G, prog="twopi")
	# return nx.spring_layout(G, iterations=500)
	return nx.kamada_kawai_layout(G)

def getLoads(path):
	print(f'Working on {path}')
	tree = dssConvert.dssToTree(path)
	loads = [obj.get('object','').split('.')[1] for obj in tree if 'load.' in obj.get('object','')]
	return loads

def _tests():
	lat = 39.7817
	lon = -89.6501
	with open('testfiles/test_params.json') as file:
		test_params = json.load(file)
	MICROGRIDS = test_params['MICROGRIDS']
	wizard_dir = [_dir for _dir in MICROGRIDS if 'wizard' in _dir]
	elements = test_params['elements']
	templateDssTree = [OrderedDict(item) for item in test_params['templateDssTree']]
	# Testing wizard_to_dss().
	for _dir in wizard_dir:
		dssFilePath = wizard_to_dss(_dir, lat, lon, elements, True, 'true')
		dssTree = dssConvert.dssToTree(dssFilePath)
		expectedDssTree = templateDssTree
		expectedDssTree[2] = OrderedDict([('!CMD', 'new'), ('object', f'circuit.{_dir.lower()}')])
		# Find index of 'makebuslist' because NetworkX shifts coordinates around each run and it is useless to compare them.
		mbl = [y for y in expectedDssTree if y.get('!CMD') == 'makebuslist']
		idx = expectedDssTree.index(mbl[0])
		assert dssTree[:idx] == expectedDssTree[:idx], f'dssTree did not match expectedDssTree when testing {_dir}.\nExpected output: {expectedDssTree[:idx]}.\nReceived output: {dssTree[:idx]}.'
	return print('Ran all tests for for microgridup_gui.py.')

# Helper app to redirect http -> https
reApp = Flask("HTTPS_REDIRECT")

@reApp.route("/")
def index():
	return "NA"

@reApp.before_request
def before_request():
	# Handle ACME challenges for letsencrypt to keep SSL renewing.
	if '/.well-known/acme-challenge' in request.url:
		try:
			filename = request.url.split('/')[-1]
		except:
			filename = 'none'
		return send_from_directory(f'{microgridup.MGU_DIR}.well-known/acme-challenge', filename)
	# Redirect http -> https
	elif request.url.startswith("http://"):
		url = request.url.replace("http://", "https://", 1)
		return redirect(url, code=301)

if __name__ == "__main__":
	if platform.system() == "Darwin":  # MacOS
		os.environ['NO_PROXY'] = '*' # Workaround for macOS proxy behavior
		multiprocessing.set_start_method('forkserver') # Workaround for Catalina exec/fork behavior
	gunicorn_args = ['gunicorn', '-w', '5', '--reload', 'microgridup_gui:app','--worker-class=sync', '--timeout=100']
	mguPath = Path(microgridup.MGU_DIR)
	if (mguPath/'ssl').exists() and (mguPath/'logs').exists():
		# if production directories, run in prod mode with logging and ssl.
		gunicorn_args.extend(['--access-logfile', mguPath / 'logs/mgu.access.log', '--error-logfile', mguPath / 'logs/mgu.error.log', '--capture-output'])
		gunicorn_args.extend(['--certfile', mguPath / 'ssl/cert.pem', '--keyfile', mguPath / 'ssl/privkey.pem', '--ca-certs', mguPath/'ssl/fullchain.pem'])
		gunicorn_args.extend(['-b', '0.0.0.0:443'])
		redirProc = Popen(['gunicorn', '-w', '5', '-b', '0.0.0.0:80', 'microgridup_gui:reApp']) # don't need to wait, only wait on main proc.
		appProc = Popen(gunicorn_args)
	else:
		# no production directories, run in dev mode, i.e. no log files, no ssl.
		# app.run(debug=True, host="0.0.0.0") # old flask way, don't use.
		gunicorn_args.extend(['-b', '0.0.0.0:5000'])
		appProc = Popen(gunicorn_args)
	appProc.wait()