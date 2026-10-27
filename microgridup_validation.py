import json, math
from types import MappingProxyType
import networkx as nx
import pandas as pd
from omf.solvers.opendss import dssConvert


'''
- Validation logic:
    - Unrecoverable errors
        - A ValueError is raised if a microgrid has no loads at all. If form_microgrids() also finds no switch for it (e.g. a microgrid that starts at
          the substation's source bus), it raises a SwitchNotFoundError instead, because it checks the switch first
        - A run is rejected if no load in the whole project is critical (run() in microgridup_gui.py returns a 400 response instead of raising)
        - A ValueError is raised if a Manual or Load Grouping group is empty, has a node that isn't in the circuit, or spans more than one
          substation's tree (or, for a wizard circuit, more than one feeder bus or none)
        - A ValueError is raised if a load is in more than one microgrid
        - A ValueError is raised if a microgrid's switch isn't a line or transformer, or opening it leaves some of the microgrid's loads connected to
          the substation
        - A ValueError is raised if a microgrid's generation bus isn't a bus, has no coordinates, or can't reach the microgrid's loads when every
          microgrid's switch is open
        - A ValueError is raised if an Outage Location isn't a line in the circuit
        - A ValueError is raised if the load CSV can't be read, doesn't have exactly 8760 rows, or lacks a column for a microgrid load
        - A ValueError is raised if the specific load growth isn't a JSON object of load CSV columns and numeric percentages of at least -100
        - A ValueError is raised if the additional load shape's meter isn't a load CSV column, or the file isn't one column of numbers with one row
          per load CSV row
        - A ValueError is raised if the outages CSV can't be read, lacks a column, has no outages, or has an outage with an unreadable date, a
          missing or negative duration, or a finish before its start
        - A ValueError is raised if a REopt run's custom URDB rate would crash REopt, have part of it ignored, or charge nothing, or if its energy
          and demand costs are both 0
    - Warnings
        - A warning is given if a microgrid has no critical loads. Preview shows it under the map, and microgridup.main() prints it and writes it to
          logs.log and user_warnings.txt
        - A warning is given if loads that are in no microgrid would be in a microgrid's island during an outage
        - A warning is given if a partition method makes a different number of microgrids than the minimum the user asked for. Only Preview shows it
        - A warning is given if the outages CSV has names that aren't in the circuit, or durations that don't match their start and finish times
        - A warning is given if a custom URDB rate has negative rates, or energy tier limits that REopt simplifies
'''

####################################################################################################################
# - Partition groups: Manual and Load Grouping check these before form_microgrids() turns the groups into microgrids
####################################################################################################################

def raise_for_empty_mg_groups(mg_groups, partition_params_json=None):
	'''
	Raise a ValueError if a microgrid group has no nodes

	:param mg_groups: the names of the nodes in each microgrid, as made by form_mg_groups(). The first group is "mg0", the second "mg1", and so on
	:type mg_groups: list
	:param partition_params_json: the PARTITION_PARAMS posted by the front end. Only its optional "mg_name" dict is read, to name the microgrids in
		the error message
	:type partition_params_json: str, dict or None
	:rtype: None
	'''
	labels = _mg_group_labels(mg_groups, partition_params_json)
	_raise_problems('The microgrids can\'t be run as defined:', [f'{labels[idx]} is empty.' for idx, group in enumerate(mg_groups) if not _group_nodes(group)])


def raise_for_mg_group_nodes_not_in_circuit(mg_groups, G, partition_params_json=None):
	'''
	Raise a ValueError if a microgrid group has a node that isn't in the circuit graph

	:param mg_groups: the names of the nodes in each microgrid, as made by form_mg_groups(). The first group is "mg0", the second "mg1", and so on
	:type mg_groups: list
	:param G: the circuit graph, as made by dssConvert.dss_to_networkx()
	:type G: networkx.DiGraph
	:param partition_params_json: the PARTITION_PARAMS posted by the front end. Only its optional "mg_name" dict is read, to name the microgrids in
		the error message
	:type partition_params_json: str, dict or None
	:rtype: None
	'''
	labels = _mg_group_labels(mg_groups, partition_params_json)
	problems = []
	for idx, group in enumerate(mg_groups):
		# - Do all nodes exist in the graph?
		missing = [n for n in _group_nodes(group) if n not in G.nodes()]
		if missing:
			problems.append(f'{labels[idx]} contains unknown nodes: {missing}')
	_raise_problems('The microgrids can\'t be run as defined:', problems)


def raise_for_mg_groups_not_in_one_tree(mg_groups, G, partition_params_json=None):
	'''
	Raise a ValueError if a microgrid group spans more than one weakly connected component of the circuit graph (its nodes aren't all reachable from
	each other even when edge directions are ignored), which means more than one substation's tree

	:param mg_groups: the names of the nodes in each microgrid, as made by form_mg_groups(). The first group is "mg0", the second "mg1", and so on
	:type mg_groups: list
	:param G: the circuit graph, as made by dssConvert.dss_to_networkx()
	:type G: networkx.DiGraph
	:param partition_params_json: the PARTITION_PARAMS posted by the front end. Only its optional "mg_name" dict is read, to name the microgrids in
		the error message
	:type partition_params_json: str, dict or None
	:rtype: None
	'''
	# Map node -> component id
	node_to_comp = {}
	for comp_id, comp_nodes in enumerate(nx.weakly_connected_components(G)):
		for n in comp_nodes:
			node_to_comp[n] = comp_id
	labels = _mg_group_labels(mg_groups, partition_params_json)
	problems = []
	for idx, group in enumerate(mg_groups):
		nodes = _group_nodes(group)
		# - Are they all in the same weakly connected component? Nodes that aren't in the graph have their own check
		comp_ids = {node_to_comp[n] for n in nodes if n in node_to_comp}
		if len(comp_ids) > 1:
			problems.append(f'{labels[idx]} spans multiple substations/trees; nodes: {nodes}')
	_raise_problems('The microgrids can\'t be run as defined:', problems)


def raise_for_mg_groups_not_on_one_feeder(mg_groups, omd, partition_params_json=None):
	'''
	Raise a ValueError if a microgrid group's objects hang off more than one parent bus (feeder), or if the group has no parent bus and no bus. The
	GUI checks this only for circuits made with the wizard

	:param mg_groups: the names of the nodes in each microgrid, as made by form_mg_groups(). The first group is "mg0", the second "mg1", and so on
	:type mg_groups: list
	:param omd: the circuit, as made by dssConvert.dssToOmd()
	:type omd: dict
	:param partition_params_json: the PARTITION_PARAMS posted by the front end. Only its optional "mg_name" dict is read, to name the microgrids in
		the error message
	:type partition_params_json: str, dict or None
	:rtype: None
	'''
	# Map omd names to objects for fast lookup
	omd_by_name = {ob.get('name'): ob for ob in omd.values()}
	labels = _mg_group_labels(mg_groups, partition_params_json)
	problems = []
	for idx, group in enumerate(mg_groups):
		nodes = _group_nodes(group)
		# - Empty groups have their own check
		if not nodes:
			continue
		# - Collect parent buses for all nodes that have them
		parent_buses = set()
		for n in nodes:
			ob = omd_by_name.get(n)
			if not ob:
				continue
			parent = ob.get('parent') or ob.get('bus') or ob.get('bus1') or ob.get('parent_bus')
			if parent:
				parent_buses.add(str(parent).split('.')[0])
		# If we found multiple distinct parent buses, group spans feeders -> problem
		if len(parent_buses) > 1:
			problems.append(f'{labels[idx]} spans multiple feeder buses: {sorted(parent_buses)}; nodes: {nodes}')
			continue
		# If we found no parent buses, ensure at least one member of the group is a bus-type object in omd
		if len(parent_buses) == 0:
			found_bus_obj = False
			for n in nodes:
				ob = omd_by_name.get(n)
				if ob and ob.get('object') and ob.get('object').lower() == 'bus':
					found_bus_obj = True
					break
			if not found_bus_obj:
				problems.append(f'{labels[idx]} has no identifiable parent bus and contains no bus object; nodes: {nodes}')
	_raise_problems('The microgrids can\'t be run as defined:', problems)


def _mg_group_labels(mg_groups, partition_params_json):
	'''
	:param mg_groups: the names of the nodes in each microgrid, as made by form_mg_groups(). The first group is "mg0", the second "mg1", and so on
	:type mg_groups: list
	:param partition_params_json: the PARTITION_PARAMS posted by the front end. Only its optional "mg_name" dict is read
	:type partition_params_json: str, dict or None
	:return: a label for each group to use in messages, like "north (mg1)"
	:rtype: list
	'''
	# Optional mg names (user-supplied via partition params)
	mg_name_map = {}
	if partition_params_json:
		try:
			params = json.loads(partition_params_json) if isinstance(partition_params_json, str) else partition_params_json
			mg_name_map = params.get('mg_name', {}) or {}
		except Exception:
			mg_name_map = {}
	labels = []
	for idx in range(len(mg_groups)):
		mg_id = f'mg{idx}'
		labels.append(f'{mg_name_map.get(mg_id, mg_id)} ({mg_id})')
	return labels


def _group_nodes(group):
	'''
	:param group: the names of the nodes in one microgrid group
	:type group: list
	:return: the names without None or empty strings
	:rtype: list
	'''
	return [n for n in group if n is not None and n != '']  # Guard against empty strings

##########################################################################################################################################
# - Finished microgrids. The checks model an outage the way microgridup_control.py does: every microgrid opens its switch at the same time
##########################################################################################################################################

def raise_for_microgrids_without_loads(data):
	'''
	Raise a ValueError if a microgrid has no loads. Every microgrid must have at least one

	:param data: the run's inputs, with the microgrid definitions under "MICROGRIDS"
	:type data: dict or MappingProxyType
	:rtype: None
	'''
	missing = [mg_name for mg_name, mg in data['MICROGRIDS'].items() if not mg.get('loads')]
	if missing:
		raise ValueError(f'The following microgrids have no loads assigned: {missing}. At least one load must be placed in each microgrid.')


def raise_for_missing_intended_microgrids(MICROGRIDS, partition_params_json):
	'''
	Raise a ValueError if a microgrid that the user named in the partitioner (PARTITION_PARAMS.mg_name) is missing from MICROGRIDS, which happens when
	no loads were assigned to it

	:param MICROGRIDS: the microgrids made from the partition parameters. form_microgrids() keys each by the user's name, or by its id ("mg0", "mg1",
		...) when it has no name
	:type MICROGRIDS: dict
	:param partition_params_json: the PARTITION_PARAMS str posted by the front end
	:type partition_params_json: str
	:rtype: None
	'''
	try:
		mg_name_map = json.loads(partition_params_json).get('mg_name', {})  # Map ids -> names: {'mg0': 'name0', 'mg1': 'name1'}
	except Exception:
		mg_name_map = {}
	# - Only run the check when the front end supplied microgrid names. A re-run from the edit page without choosing a partition method sends the
	#   saved MICROGRIDS dict as PARTITION_PARAMS, which has no "mg_name" key
	if not mg_name_map:
		return
	actual_mg_ids = {mg_id for mg_id, name in mg_name_map.items() if mg_id in MICROGRIDS or (name and name in MICROGRIDS)}
	missing_names = [mg_name_map[mg_id] or mg_id for mg_id in sorted(set(mg_name_map) - actual_mg_ids)]
	if missing_names:
		raise ValueError(f'The following microgrids were defined but no loads were assigned: {missing_names}. Either assign at least one load to each '
			'microgrid or remove them from the partitioner and try again.')


def raise_for_invalid_microgrids(MICROGRIDS, omd, partition_method=''):
	'''
	Run every check that stops the microgrids from being run, in order: a load in more than one microgrid, a switch that doesn't island its microgrid,
	a generation bus that isn't a bus the microgrid's loads can reach. The GUI calls it at Preview and Run, and microgridup.main() when a run starts

	:param MICROGRIDS: the microgrid definitions keyed by microgrid name, as made by form_microgrids()
	:type MICROGRIDS: dict or MappingProxyType
	:param omd: the circuit, as made by dssConvert.dssToOmd()
	:type omd: dict
	:param partition_method: the partition method that made the microgrids, or '' if unknown. Only used to word the messages
	:type partition_method: str
	:rtype: None
	'''
	raise_for_loads_in_multiple_microgrids(MICROGRIDS, partition_method)
	raise_for_invalid_switches(MICROGRIDS, omd)
	raise_for_invalid_gen_buses(MICROGRIDS, omd)


def raise_for_loads_in_multiple_microgrids(MICROGRIDS, partition_method=''):
	'''
	Raise a ValueError if a load is in more than one microgrid

	:param MICROGRIDS: the microgrid definitions keyed by microgrid name, as made by form_microgrids()
	:type MICROGRIDS: dict or MappingProxyType
	:param partition_method: the partition method that made the microgrids, or '' if unknown. Only used to word the message
	:type partition_method: str
	:rtype: None
	'''
	microgrids_by_load = _microgrids_by_load(MICROGRIDS)
	shared_loads = sorted(load for load, mg_names in microgrids_by_load.items() if len(mg_names) > 1)
	if not shared_loads:
		return
	msg = 'Each load can belong to only one microgrid, but ' + '; '.join(f'"{load}" is in {_name_list(microgrids_by_load[load])}' for load in shared_loads[:8])
	if len(shared_loads) > 8:
		msg += f'; and {len(shared_loads) - 8} more loads are shared'
	msg += '.'
	if partition_method == 'loadGrouping':
		msg += ' Load Grouping adds every load downstream of the bus where a microgrid\'s loads meet, so microgrids whose loads meet at the same bus overlap. Use Manual partitioning to keep exactly the loads you chose.'
	_raise_problems('The microgrids can\'t be run as defined:', [msg])


def raise_for_invalid_switches(MICROGRIDS, omd):
	'''
	Raise a ValueError if a microgrid's switch isn't a line or transformer, or if opening it leaves some of the microgrid's loads connected to the
	substation, so the microgrid can't island

	:param MICROGRIDS: the microgrid definitions keyed by microgrid name, as made by form_microgrids()
	:type MICROGRIDS: dict or MappingProxyType
	:param omd: the circuit, as made by dssConvert.dssToOmd()
	:type omd: dict
	:rtype: None
	'''
	G, edges_by_name = _circuit_graph(omd)
	substations = _substation_buses(omd, G)
	problems = [_switch_problem(mg_name, mg, G, edges_by_name, substations) for mg_name, mg in MICROGRIDS.items()]
	_raise_problems('The microgrids can\'t be run as defined:', [problem for problem in problems if problem])


def raise_for_invalid_gen_buses(MICROGRIDS, omd):
	'''
	Raise a ValueError if a microgrid's generation bus isn't a bus, has no coordinates (REopt needs the site location), or can't reach the
	microgrid's loads when every microgrid's switch is open. The last check skips a microgrid whose switch can't island it, which
	raise_for_invalid_switches() reports

	:param MICROGRIDS: the microgrid definitions keyed by microgrid name, as made by form_microgrids()
	:type MICROGRIDS: dict or MappingProxyType
	:param omd: the circuit, as made by dssConvert.dssToOmd()
	:type omd: dict
	:rtype: None
	'''
	omd_by_name = {ob.get('name'): ob for ob in omd.values() if ob.get('name')}
	buses = {name: ob for name, ob in omd_by_name.items() if ob.get('object') == 'bus'}
	G, edges_by_name = _circuit_graph(omd)
	substations = _substation_buses(omd, G)
	islands = _outage_islands(MICROGRIDS, G, edges_by_name)
	shared_loads = {load for load, mg_names in _microgrids_by_load(MICROGRIDS).items() if len(mg_names) > 1}
	problems = []
	for mg_name, mg in MICROGRIDS.items():
		loads = set(mg.get('loads', []))
		gen_bus = mg.get('gen_bus')
		if gen_bus not in buses:
			msg = f'Microgrid "{mg_name}": the generation bus "{gen_bus}" is not a bus in the circuit.'
			load_buses = {str(omd_by_name[load]['parent']).split('.')[0] for load in loads if omd_by_name.get(load, {}).get('parent')}
			# - form_microgrids() falls back to the microgrid's first load or generator when its loads are on more than one bus
			if (gen_bus in loads or gen_bus in mg.get('gen_obs_existing', [])) and len(load_buses) > 1:
				msg += f' Its loads are on more than one bus ({_name_list(load_buses)}), so a generation bus can\'t be chosen automatically. Enter one in the microgrid\'s Gen Bus input.'
			else:
				msg += ' Check the name in the microgrid\'s Gen Bus input.'
			problems.append(msg)
		elif not (buses[gen_bus].get('latitude') and buses[gen_bus].get('longitude')):
			problems.append(f'Microgrid "{mg_name}": the generation bus "{gen_bus}" has no coordinates in the circuit file, and REopt needs them for the site location.')
		elif not _switch_problem(mg_name, mg, G, edges_by_name, substations):
			island = nx.node_connected_component(islands, gen_bus) if gen_bus in islands else {gen_bus}
			# - Shared loads have their own error
			unreachable = loads - island - shared_loads
			if unreachable:
				problems.append(
					f'Microgrid "{mg_name}": when the microgrids\' switches open, {_name_list(unreachable)} can\'t be reached from the generation bus '
					f'"{gen_bus}". Choose a generation bus on the microgrid\'s side of its switch "{_microgrid_switch(mg)}" that no other microgrid\'s switch '
					'cuts off.'
				)
	_raise_problems('The microgrids can\'t be run as defined:', problems)


def get_outside_load_warnings(MICROGRIDS, omd):
	'''
	Warn about loads that are in no microgrid but would be in a microgrid's island during an outage. The microgrid's design doesn't plan power for
	them. Microgrids whose switch or generation bus has a problem are skipped, because raise_for_invalid_switches() and raise_for_invalid_gen_buses() report them

	:param MICROGRIDS: the microgrid definitions keyed by microgrid name, as made by form_microgrids()
	:type MICROGRIDS: dict or MappingProxyType
	:param omd: the circuit, as made by dssConvert.dssToOmd()
	:type omd: dict
	:return: warning messages for the user
	:rtype: list
	'''
	omd_by_name = {ob.get('name'): ob for ob in omd.values() if ob.get('name')}
	buses = {name: ob for name, ob in omd_by_name.items() if ob.get('object') == 'bus'}
	G, edges_by_name = _circuit_graph(omd)
	substations = _substation_buses(omd, G)
	islands = _outage_islands(MICROGRIDS, G, edges_by_name)
	all_loads = {name for name, ob in omd_by_name.items() if ob.get('object') == 'load' and name in islands}
	loads_in_microgrids = set(_microgrids_by_load(MICROGRIDS))
	warnings = []
	for mg_name, mg in MICROGRIDS.items():
		gen_bus = mg.get('gen_bus')
		if gen_bus not in buses or _switch_problem(mg_name, mg, G, edges_by_name, substations):
			continue
		island = nx.node_connected_component(islands, gen_bus) if gen_bus in islands else {gen_bus}
		outside_loads = (island & all_loads) - loads_in_microgrids
		if outside_loads:
			warnings.append(
				f'Microgrid "{mg_name}": loads that are in no microgrid ({_name_list(outside_loads)}) would be in this microgrid\'s island during an '
				'outage, and its design doesn\'t plan power for them. Add them to the microgrid or choose a switch that keeps them on the substation side.'
			)
	return warnings


def get_no_critical_load_warnings(MICROGRIDS, critical_loads):
	'''
	Warn about microgrids with no critical loads, since there is nothing for them to keep powered during an outage

	:param MICROGRIDS: the microgrid definitions keyed by microgrid name, as made by form_microgrids()
	:type MICROGRIDS: dict or MappingProxyType
	:param critical_loads: the names of the critical loads
	:type critical_loads: list or tuple
	:return: warning messages for the user
	:rtype: list
	'''
	return [f'Microgrid "{mg_name}" has no critical loads, so there is nothing for it to keep powered during an outage.'
		for mg_name, mg in MICROGRIDS.items() if not set(mg.get('loads', [])) & set(critical_loads)]


def get_microgrid_count_warnings(MICROGRIDS, partition_method, requested_quantity):
	'''
	Warn when the partition method made a different number of microgrids than the minimum the user asked for

	:param MICROGRIDS: the microgrid definitions keyed by microgrid name, as made by form_microgrids()
	:type MICROGRIDS: dict or MappingProxyType
	:param partition_method: the partition method that made the microgrids, or '' if unknown. Only used to word the message
	:type partition_method: str
	:param requested_quantity: the minimum number of microgrids the user asked for, or 0 if the partition method doesn't take one
	:type requested_quantity: int
	:return: warning messages for the user
	:rtype: list
	'''
	if not requested_quantity or len(MICROGRIDS) == requested_quantity:
		return []
	method_names = {'branch': 'Branch', 'bottomUp': 'Bottom-Up', 'criticalLoads': 'Critical Loads', 'loadGrouping': 'Load Grouping', 'manual': 'Manual', 'lukes': 'Lukes'}
	method_name = method_names.get(partition_method, 'the partition method')
	if len(MICROGRIDS) > requested_quantity:
		return [f'You asked for at least {requested_quantity} microgrids, and {method_name} made {len(MICROGRIDS)}.']
	return [f'You asked for at least {requested_quantity} microgrids, but {method_name} could only make {len(MICROGRIDS)}.']


def _microgrids_by_load(MICROGRIDS):
	'''
	:param MICROGRIDS: the microgrid definitions keyed by microgrid name
	:type MICROGRIDS: dict or MappingProxyType
	:return: the names of the microgrids that each load is in, keyed by load name
	:rtype: dict
	'''
	microgrids_by_load = {}
	for mg_name, mg in MICROGRIDS.items():
		for load in mg.get('loads', []):
			microgrids_by_load.setdefault(load, []).append(mg_name)
	return microgrids_by_load


def _circuit_graph(omd):
	'''
	Build the circuit graph the way dssConvert.dss_to_networkx() does: lines and transformers are edges between buses, and loads, generators, etc.
	hang off their parent bus. Phase suffixes (e.g. "675.1.2") are dropped so every reference to a bus meets at one node

	:param omd: the circuit, as made by dssConvert.dssToOmd()
	:type omd: dict
	:return: the graph, and the edge of each line and transformer keyed by its name
	:rtype: tuple
	'''
	G = nx.DiGraph()
	edges_by_name = {}
	for ob in omd.values():
		if 'from' in ob and 'to' in ob:
			edge = (str(ob['from']).split('.')[0], str(ob['to']).split('.')[0])
			G.add_edge(*edge)
			edges_by_name.setdefault(ob.get('name'), edge)
		if 'name' in ob and 'parent' in ob and ob.get('object') not in ('circuit', 'vsource'):
			G.add_edge(str(ob['parent']).split('.')[0], ob['name'])
	return G, edges_by_name


def _substation_buses(omd, G):
	'''
	The substation is the bus of the circuit's source. A second source further down the feeder (e.g. behind a normally open tie switch, which the
	graph shows as closed) has an edge coming in and is skipped. Without a source bus, use the roots of the graph

	:param omd: the circuit, as made by dssConvert.dssToOmd()
	:type omd: dict
	:param G: the circuit graph, as made by _circuit_graph()
	:type G: networkx.DiGraph
	:return: the substation buses
	:rtype: set
	'''
	source_buses = {str(ob.get('parent')).split('.')[0] for ob in omd.values() if ob.get('object') in ('circuit', 'vsource')}
	substations = {n for n in source_buses if n in G and G.in_degree(n) == 0}
	if not substations:
		substations = {n for n in G.nodes() if G.in_degree(n) == 0 and G.out_degree(n) > 0}
	return substations


def _switch_problem(mg_name, mg, G, edges_by_name, substations):
	'''
	:param mg_name: the name of the microgrid
	:type mg_name: str
	:param mg: the microgrid definition
	:type mg: dict or MappingProxyType
	:param G: the circuit graph, as made by _circuit_graph()
	:type G: networkx.DiGraph
	:param edges_by_name: the edge of each line and transformer keyed by its name, as made by _circuit_graph()
	:type edges_by_name: dict
	:param substations: the substation buses, as found by _substation_buses()
	:type substations: set
	:return: why the microgrid's switch can't island it, or '' if it can
	:rtype: str
	'''
	switch = _microgrid_switch(mg)
	if switch not in edges_by_name:
		return f'Microgrid "{mg_name}": the switch "{switch}" is not a line or transformer in the circuit.'
	# - Open only this microgrid's switch and see which of its loads can still reach a substation
	opened = G.to_undirected()
	opened.remove_edge(*edges_by_name[switch])
	connected = set()
	for substation in substations:
		connected |= nx.node_connected_component(opened, substation)
	still_connected = set(mg.get('loads', [])) & connected
	if still_connected:
		return (f'Microgrid "{mg_name}": opening switch "{switch}" leaves {_name_list(still_connected)} connected to the substation, so the microgrid '
			'can\'t island. Choose a switch upstream of all of its loads (loads on different branches need a switch above the bus where the branches '
			'meet), or put those loads in a different microgrid.')
	return ''


def _microgrid_switch(mg):
	'''
	Normalize a microgrid's switch the way microgridup_control.get_first_nodes_of_mgs() does: a list of switches becomes its last non-empty switch

	:param mg: one microgrid definition
	:type mg: dict or MappingProxyType
	:return: the name of the microgrid's switch
	:rtype: str
	'''
	switch = mg.get('switch')
	if isinstance(switch, (list, tuple)):
		switch = next((s for s in reversed(switch) if s), '')
	return switch


def _outage_islands(MICROGRIDS, G, edges_by_name):
	'''
	:param MICROGRIDS: the microgrid definitions keyed by microgrid name
	:type MICROGRIDS: dict or MappingProxyType
	:param G: the circuit graph, as made by _circuit_graph()
	:type G: networkx.DiGraph
	:param edges_by_name: the edge of each line and transformer keyed by its name, as made by _circuit_graph()
	:type edges_by_name: dict
	:return: the islands during an outage: the circuit, ignoring edge directions, with every microgrid's switch open
	:rtype: networkx.Graph
	'''
	islands = G.to_undirected()
	switches = {_microgrid_switch(mg) for mg in MICROGRIDS.values()}
	islands.remove_edges_from([edges_by_name[s] for s in switches if s in edges_by_name])
	return islands

####################
# - Outage locations
####################

def raise_for_invalid_outage_locations(faulted_lines, omd):
	'''
	Raise a ValueError if an outage location isn't a line in the circuit. The control simulation opens each outage location with
	"open object=line.<name>", and OpenDSS stops the whole simulation script at a name that isn't a line

	:param faulted_lines: the names in the user's Outage Location(s) input
	:type faulted_lines: list or tuple
	:param omd: the circuit, as made by dssConvert.dssToOmd()
	:type omd: dict
	:rtype: None
	'''
	not_lines = _outage_locations_that_are_not_lines(faulted_lines, omd)
	if len(not_lines) == 1:
		raise ValueError(f'The Outage Location {_name_list(not_lines)} isn\'t a line in the circuit. Enter the names of lines where a typical outage '
			'would take place, separated by commas.')
	if not_lines:
		raise ValueError(f'The Outage Locations {_name_list(not_lines)} aren\'t lines in the circuit. Enter the names of lines where a typical outage '
			'would take place, separated by commas.')


def faulted_lines_in_graph(path_to_dss, faulted_lines):
	'''
	Return whether every faulted line name is a line in the DSS file. OpenDSS names aren't case-sensitive, so neither is the match

	:param path_to_dss: the path to circuit_plus_mgAll.dss, the circuit with all of the REopt-recommended generation objects added, in which the
		control simulation opens the outage locations
	:type path_to_dss: str
	:param faulted_lines: the names of the lines that the control simulation opens to start the outage (the user's Outage Location input)
	:type faulted_lines: tuple
	:return: True if every name in faulted_lines is the name of a line in the circuit, else False
	:rtype: bool
	'''
	assert isinstance(path_to_dss, str)
	assert isinstance(faulted_lines, tuple)
	omd = dssConvert.dssToOmd(path_to_dss, '', write_out=False)
	return not _outage_locations_that_are_not_lines(faulted_lines, omd)


def _outage_locations_that_are_not_lines(faulted_lines, omd):
	'''
	:param faulted_lines: the names in the user's Outage Location(s) input
	:type faulted_lines: list or tuple
	:param omd: the circuit, as made by dssConvert.dssToOmd()
	:type omd: dict
	:return: the names that aren't lines in the circuit. OpenDSS names aren't case-sensitive, so neither is the match
	:rtype: list
	'''
	line_names = {str(ob.get('name')).lower() for ob in omd.values() if ob.get('object') == 'line'}
	return [name for name in faulted_lines if str(name).lower() not in line_names]

######################################################################################################
# - The load CSV and the inputs that change it: the specific load growth and the additional load shape
######################################################################################################

def raise_for_invalid_loads_csv(load_csv_path, MICROGRIDS):
	'''
	Raise a ValueError if the load CSV doesn't have one year of hourly data or has no column for one of the microgrid loads

	:param load_csv_path: the path to the load CSV
	:type load_csv_path: str
	:param MICROGRIDS: the microgrid definitions keyed by microgrid name
	:type MICROGRIDS: dict or MappingProxyType
	:rtype: None
	'''
	load_df = _read_load_csv(load_csv_path)
	problems = []
	# - One year of hourly data: omf's microgridDesign model stops with "Loadshape CSV file is incorrect format" for any other number of rows, so
	#   every REopt run would fail
	if len(load_df) != 8760:
		problems.append(f'It must have exactly 8760 rows of hourly data after its heading row, but it has {len(load_df)}.')
	# Normalize columns (case-insensitive matching)
	load_columns_lower = [str(c).lower() for c in load_df.columns]
	for mg_name, mg in MICROGRIDS.items():
		# Validate that the loads required by this microgrid exist in the CSV.
		missing = [load for load in mg.get('loads', []) if load and str(load).lower() not in load_columns_lower]
		if missing:
			problems.append(f'Loads CSV is missing required columns for microgrid "{mg_name}". Missing load columns: {list(missing)}. '
				'The loads.csv header must contain load names that match the circuit (case-insensitive). '
				'Please upload a loads.csv that corresponds to the selected circuit.')
	_raise_problems('The load CSV can\'t be used:', problems)


def raise_for_invalid_load_growth(load_growth_specific, load_csv_path):
	'''
	Raise a ValueError if the specific load growth can't be applied. apply_load_growth() in microgridup_design.py multiplies each named column of
	the load CSV by (1 + percentage / 100), but it only logs a meter that isn't a column to logs.log and skips it, and a percentage that isn't a
	number crashes it

	:param load_growth_specific: the user's specific load growth, which maps meter names (columns of the load CSV) to percentages
	:type load_growth_specific: dict or MappingProxyType
	:param load_csv_path: the path to the load CSV
	:type load_csv_path: str
	:rtype: None
	'''
	if not isinstance(load_growth_specific, (dict, MappingProxyType)):
		raise ValueError('The "Specific Load Growth (JSON format)" must be a JSON object that maps meter names to percentages, like {"meter1": 10}.')
	load_columns = _read_load_csv(load_csv_path).columns
	problems = []
	unknown_meters = [meter for meter in load_growth_specific if meter not in load_columns]
	if len(unknown_meters) == 1:
		problems.append(f'The meter {_name_list(unknown_meters)} isn\'t a column of the load CSV. Meter names must match its heading row exactly.')
	elif unknown_meters:
		problems.append(f'The meters {_name_list(unknown_meters)} aren\'t columns of the load CSV. Meter names must match its heading row exactly.')
	not_numbers = [meter for meter, percent in load_growth_specific.items() if not _is_number(percent)]
	if len(not_numbers) == 1:
		problems.append(f'The percentage for {_name_list(not_numbers)} isn\'t a number.')
	elif not_numbers:
		problems.append(f'The percentages for {_name_list(not_numbers)} aren\'t numbers.')
	# - Growth of -100% makes a load 0, and anything lower makes it negative
	too_low = [meter for meter, percent in load_growth_specific.items() if meter not in not_numbers and percent < -100]
	if len(too_low) == 1:
		problems.append(f'The percentage for {_name_list(too_low)} is below -100, which would make its load negative.')
	elif too_low:
		problems.append(f'The percentages for {_name_list(too_low)} are below -100, which would make their loads negative.')
	_raise_problems('The "Specific Load Growth (JSON format)" can\'t be used:', problems)


def raise_for_invalid_additional_loadshape(loadshape_csv_path, meter, load_csv_path):
	'''
	Raise a ValueError if the additional load shape can't be added to its meter. apply_load_growth() in microgridup_design.py reads the file with no
	heading row and adds its one column to the meter's column of the load CSV, but it only logs a problem to logs.log and skips the load shape

	:param loadshape_csv_path: the path to the additional load shape CSV
	:type loadshape_csv_path: str
	:param meter: the column of the load CSV that the load shape is added to
	:type meter: str
	:param load_csv_path: the path to the load CSV
	:type load_csv_path: str
	:rtype: None
	'''
	load_df = _read_load_csv(load_csv_path)
	problems = []
	if not meter:
		problems.append('No meter was chosen for it in "Meter for Additional Loadshape".')
	elif meter not in load_df.columns:
		problems.append(f'Its meter "{meter}" isn\'t a column of the load CSV.')
	try:
		loadshape_df = pd.read_csv(loadshape_csv_path, header=None)
	except Exception as e:
		problems.append(f'It can\'t be read as a CSV file ({e}).')
	else:
		if len(loadshape_df.columns) != 1:
			problems.append(f'It must have exactly one column, but it has {len(loadshape_df.columns)}.')
		if len(loadshape_df) != len(load_df):
			problems.append(f'It must have {len(load_df)} rows, one for each row of the load CSV, but it has {len(loadshape_df)}.')
		# - The file has no heading row, so its first row is line 1
		not_numbers = pd.to_numeric(loadshape_df.iloc[:, 0], errors='coerce').isna()
		if not_numbers.any():
			lines = _line_list(i + 1 for i in loadshape_df.index[not_numbers])
			verb = 'isn\'t a number' if not_numbers.sum() == 1 else 'aren\'t numbers'
			heading_hint = ' It must not have a heading row.' if not_numbers.iloc[0] else ''
			problems.append(f'{lines[0].upper()}{lines[1:]} of its first column {verb}.{heading_hint}')
	_raise_problems('The "Additional Loadshape (.csv)" can\'t be used:', problems)


def _read_load_csv(load_csv_path):
	'''
	:param load_csv_path: the path to the load CSV
	:type load_csv_path: str
	:return: the contents of the load CSV
	:rtype: DataFrame
	:raises ValueError: if pandas can't read it
	'''
	try:
		return pd.read_csv(load_csv_path)
	except Exception as e:
		raise ValueError(f'The load CSV can\'t be read as a CSV file: {e}')

#####################################################################################
# - The historical outages CSV, which the resilience step reads at the end of the run
#####################################################################################

def raise_for_invalid_outages_csv(outages_csv_path):
	'''
	Raise a ValueError if the outages CSV would crash the resilience step or make its statistics meaningless: a date that pandas can't read or a
	duration that isn't a number crashes it, and a missing, negative or backwards outage makes its statistics meaningless

	:param outages_csv_path: the path to the outages CSV
	:type outages_csv_path: str
	:rtype: None
	'''
	outages = _read_outages_csv(outages_csv_path)
	problems = []
	times = {}
	for column in ('Start', 'Finish'):
		try:
			times[column] = pd.to_datetime(outages[column])
		except (ValueError, TypeError) as e:
			problems.append(f'The "{column}" column has a value that isn\'t a date and time ({e}).')
			continue
		if times[column].isna().any():
			problems.append(f'The "{column}" column is empty on {_outage_line_list(outages, times[column].isna())}.')
	duration = pd.to_numeric(outages['Duration_min'], errors='coerce')
	if duration.isna().any():
		problems.append(f'The "Duration_min" column is empty or not a number on {_outage_line_list(outages, duration.isna())}.')
	if (duration < 0).any():
		problems.append(f'The "Duration_min" column is negative on {_outage_line_list(outages, duration < 0)}.')
	if len(times) == 2 and (times['Finish'] < times['Start']).any():
		problems.append(f'The outage finishes before it starts on {_outage_line_list(outages, times["Finish"] < times["Start"])}.')
	_raise_problems('The outages CSV can\'t be used:', problems)


def get_outages_csv_warnings(outages_csv_path, omd):
	'''
	Warn about parts of the outages CSV that don't crash the resilience step but skew its results: durations that don't match their start and
	finish times, and names that aren't in the circuit. Call raise_for_invalid_outages_csv() first

	:param outages_csv_path: the path to the outages CSV
	:type outages_csv_path: str
	:param omd: the circuit, as made by dssConvert.dssToOmd()
	:type omd: dict
	:return: warning messages for the user
	:rtype: list
	'''
	outages = _read_outages_csv(outages_csv_path)
	warnings = []
	# - The resilience statistics use Duration_min, but the microgrid adjustment uses Start and Finish
	minutes = (pd.to_datetime(outages['Finish']) - pd.to_datetime(outages['Start'])).dt.total_seconds() / 60
	mismatched = (minutes - pd.to_numeric(outages['Duration_min'], errors='coerce')).abs() > 1
	if mismatched.any():
		warnings.append(f'Outages CSV: "Duration_min" differs from the time between "Start" and "Finish" by more than a minute on '
			f'{_outage_line_list(outages, mismatched)}. The resilience statistics use "Duration_min", but the microgrid adjustments use "Start" and "Finish".')
	# - Names that aren't in the circuit
	load_names = {ob.get('name') for ob in omd.values() if ob.get('object') == 'load'}
	unknown_meters = {meter for meters in outages['Meters Affected'].dropna() for meter in str(meters).split()} - load_names
	if unknown_meters:
		warnings.append(f'Outages CSV: the "Meters Affected" column names {_name_list(unknown_meters)}, which aren\'t loads in the circuit, so the '
			'resilience results count them as noncritical loads outside every microgrid. Meter names must match the circuit\'s load names and be '
			'separated by spaces.')
	component_names = {ob.get('name') for ob in omd.values() if ob.get('object') in ('line', 'transformer')}
	unknown_components = set(outages['ComponentAff'].dropna().astype(str)) - component_names
	if unknown_components:
		warnings.append(f'Outages CSV: the "ComponentAff" column names {_name_list(unknown_components)}, which aren\'t lines or transformers in the '
			'circuit, so the resilience results treat those outages as outside every microgrid and shorten or remove them for microgrid loads.')
	return warnings


def _read_outages_csv(outages_csv_path):
	'''
	:param outages_csv_path: the path to the outages CSV
	:type outages_csv_path: str
	:return: the outages
	:rtype: DataFrame
	:raises ValueError: if pandas can't read it, it lacks a column that microgridup_resilience.main() reads by name, or it has no outages
	'''
	required_columns = ['ComponentAff', 'Start', 'Finish', 'Duration_min', 'Meters Affected']
	try:
		outages = pd.read_csv(outages_csv_path)
	except Exception as e:
		raise ValueError(f'The outages CSV can\'t be read as a CSV file: {e}')
	missing_columns = [column for column in required_columns if column not in outages.columns]
	if missing_columns:
		noun = 'column' if len(missing_columns) == 1 else 'columns'
		raise ValueError(f'The outages CSV lacks the {noun} {_name_list(missing_columns)}. Its header must be: {",".join(required_columns)}')
	if outages.empty:
		raise ValueError('The outages CSV has a header but no outages.')
	return outages


def _outage_line_list(outages, mask):
	'''
	:param outages: the outages, as read by _read_outages_csv()
	:type outages: DataFrame
	:param mask: which rows to name
	:type mask: Series
	:return: the rows as lines of the file, like "lines 2, 3". The header is line 1, so the first outage is line 2
	:rtype: str
	'''
	return _line_list(i + 2 for i in outages.index[mask])

######################################################################################
# - Electricity rates. A custom URDB rate is checked the way urdb.jl in REopt reads it
######################################################################################

def raise_for_invalid_urdb_response(urdb_response_text):
	'''
	Raise a ValueError if the user's pasted custom URDB rate is not a rate that REopt can use: not a JSON object, missing a key that REopt requires,
	or a structure that crashes REopt, makes it ignore part of the rate, or bills nothing. The ValueError handler at the top of microgridup_gui.py
	turns it into the JSON 400 response that the submit alert shows

	:param urdb_response_text: the user's pasted JSON, or blank text for no custom rate
	:type urdb_response_text: str
	:rtype: None
	'''
	# - An empty textarea means "no custom rate". It is the default (the blended energy and demand rates are used), so it must not be parsed as JSON
	if urdb_response_text.strip() == '':
		return
	try:
		urdb_response = json.loads(urdb_response_text)
	except json.JSONDecodeError as e:
		raise ValueError(f'The "Custom URDB Rate (JSON format)" is not valid JSON: {e}')
	# - Only a JSON object can be a rate. Without this check, a pasted list or number would crash the key check below
	if not isinstance(urdb_response, dict):
		raise ValueError('The "Custom URDB Rate (JSON format)" must be a JSON object (the rate itself, not a list)')
	# urdb.jl in REopt requires these three keys
	required_keys = ('energyratestructure', 'energyweekdayschedule', 'energyweekendschedule')
	missing_keys = ', '.join([k for k in required_keys if k not in urdb_response])
	if missing_keys != '':
		raise ValueError(f'The "Custom URDB Rate (JSON format)" lacks required key(s): {missing_keys}')
	# - Check the rest of the rate the way urdb.jl reads it
	errors, _ = _check_urdb_rate(urdb_response)
	_raise_problems('The "Custom URDB Rate (JSON format)" can\'t be used:', errors)


def raise_for_invalid_rates(REOPT_INPUTS, MICROGRIDS):
	'''
	Raise a ValueError if the electricity rate of a REopt run can't be used. A custom URDB rate gets the checks in raise_for_invalid_urdb_response(). Energy
	and demand costs that are both 0 make the bill without a microgrid $0, and omf's microgridDesign model divides by that bill. Each microgrid that
	overrides the project's rate is checked too

	:param REOPT_INPUTS: the project's REopt inputs
	:type REOPT_INPUTS: dict or MappingProxyType
	:param MICROGRIDS: the microgrid definitions keyed by microgrid name, with each microgrid's REopt overrides under
		["parameter_overrides"]["reopt_inputs"]
	:type MICROGRIDS: dict or MappingProxyType
	:rtype: None
	'''
	problems = []
	for label, inputs in _rate_inputs_by_run(REOPT_INPUTS, MICROGRIDS):
		rate_text = _custom_rate_text(inputs)
		# - A custom rate replaces the energy and demand costs
		if rate_text.strip() != '':
			try:
				raise_for_invalid_urdb_response(rate_text)
			except ValueError as e:
				problems.append(f'{label}: {e}')
			continue
		try:
			energy_cost = float(inputs.get('energyCost'))
			demand_cost = float(inputs.get('demandCost'))
		except (TypeError, ValueError):
			problems.append(f'{label}: the Energy Cost ($/kWh) and Demand Cost ($/kW) must be numbers.')
			continue
		if energy_cost == 0 and demand_cost == 0:
			problems.append(f'{label}: the Energy Cost ($/kWh) and Demand Cost ($/kW) are both 0, so the bill without a microgrid is $0, and omf\'s '
				'microgridDesign model divides by that bill. Enter a cost, or a custom URDB rate.')
	if problems:
		msg = '\n'.join(problems)
		print(msg)
		raise ValueError(msg)


def get_rate_warnings(REOPT_INPUTS, MICROGRIDS):
	'''
	Warn about parts of each custom URDB rate that REopt simplifies or that are probably mistakes. Call raise_for_invalid_rates() first

	:param REOPT_INPUTS: the project's REopt inputs
	:type REOPT_INPUTS: dict or MappingProxyType
	:param MICROGRIDS: the microgrid definitions keyed by microgrid name, with each microgrid's REopt overrides under
		["parameter_overrides"]["reopt_inputs"]
	:type MICROGRIDS: dict or MappingProxyType
	:return: warning messages for the user
	:rtype: list
	'''
	warnings = []
	for label, inputs in _rate_inputs_by_run(REOPT_INPUTS, MICROGRIDS):
		rate_text = _custom_rate_text(inputs)
		if rate_text.strip() != '':
			warnings.extend(f'{label}: {warning}' for warning in _check_urdb_rate(json.loads(rate_text))[1])
	return warnings


def _check_urdb_rate(urdb_response):
	'''
	Check a custom URDB rate the way urdb.jl in REopt reads it

	:param urdb_response: the parsed rate, which already has the three keys that REopt requires
	:type urdb_response: dict
	:return: the errors (what would crash REopt, make it ignore part of the rate, or make the bill $0) and the warnings (what REopt simplifies or
		what is probably a mistake)
	:rtype: tuple
	'''
	energy_errors, (energy, energy_weekday, energy_weekend) = _energy_rate_errors(urdb_response)
	tou_errors, tou_demand = _tou_demand_rate_errors(urdb_response)
	flat_errors, flat_demand = _flat_demand_rate_errors(urdb_response)
	charge_errors, fixed_charge, min_charge = _fixed_charge_errors(urdb_response)
	errors = energy_errors + tou_errors + flat_errors + charge_errors + _lookback_errors(urdb_response)
	if errors:
		return errors, []
	# - The rest needs a rate that REopt can read: what it bills for every tier of the periods that the schedules use
	billed = _billed_rates('energyratestructure', energy, {period for month in energy_weekday + energy_weekend for period in month})
	if tou_demand:
		structure, weekday, weekend = tou_demand
		billed += _billed_rates('demandratestructure', structure, {period for month in weekday + weekend for period in month})
	if flat_demand:
		structure, months = flat_demand
		billed += _billed_rates('flatdemandstructure', structure, set(months))
	# - A rate that charges nothing makes the bill without a microgrid $0, and omf's microgridDesign model divides by that bill
	if not any(rate > 0 for _, rate in billed) and fixed_charge <= 0 and min_charge <= 0:
		return ['The rate charges nothing (every rate and fixed charge is 0), so the bill without a microgrid is $0, and omf\'s microgridDesign '
			'model divides by that bill.'], []
	warnings = []
	negative = [where for where, rate in billed if rate < 0]
	if negative:
		warnings.append(f'Some rates are below 0 ({", ".join(negative[:4])}{" and more" if len(negative) > 4 else ""}), so REopt is paid to buy that '
			'energy or demand.')
	return [], warnings + _tier_limit_warnings(energy, energy_weekday, energy_weekend)


def _energy_rate_errors(urdb_response):
	'''
	Check the energy charges, which every rate has: the structure, the unit of each tier, and the weekday and weekend schedules

	:param urdb_response: the parsed rate
	:type urdb_response: dict
	:return: the errors, and the structure and the weekday and weekend schedules (each None if it has errors)
	:rtype: tuple
	'''
	errors, structure = _rate_structure_errors(urdb_response, 'energyratestructure')
	for p, period in enumerate(structure or []):
		for t, tier in enumerate(period):
			if 'unit' in tier and str(tier['unit']) != 'kWh':
				errors.append(f'"energyratestructure" period {p} tier {t + 1}: "unit" is {json.dumps(tier["unit"])}, but REopt only reads energy rates in '
					'"kWh".')
	n_periods = len(structure) if structure else None
	weekday_errors, weekday = _rate_schedule_errors(urdb_response, 'energyweekdayschedule', n_periods)
	weekend_errors, weekend = _rate_schedule_errors(urdb_response, 'energyweekendschedule', n_periods)
	return errors + weekday_errors + weekend_errors, (structure, weekday, weekend)


def _tou_demand_rate_errors(urdb_response):
	'''
	Check the time-of-use demand charges, which are optional. Their structure needs its own weekday and weekend schedules

	:param urdb_response: the parsed rate
	:type urdb_response: dict
	:return: the errors, and the structure and the weekday and weekend schedules (each None if it has errors), or None if the rate has no
		time-of-use demand charges
	:rtype: tuple
	'''
	if 'demandratestructure' not in urdb_response:
		return [], None
	errors, structure = _rate_structure_errors(urdb_response, 'demandratestructure')
	n_periods = len(structure) if structure else None
	weekday_errors, weekday = _rate_schedule_errors(urdb_response, 'demandweekdayschedule', n_periods)
	weekend_errors, weekend = _rate_schedule_errors(urdb_response, 'demandweekendschedule', n_periods)
	return errors + weekday_errors + weekend_errors, (structure, weekday, weekend)


def _flat_demand_rate_errors(urdb_response):
	'''
	Check the flat (monthly) demand charges, which are optional. Their structure needs "flatdemandmonths", the structure's period for each month

	:param urdb_response: the parsed rate
	:type urdb_response: dict
	:return: the errors, and the structure and the period of each month (each None if it has errors), or None if the rate has no flat demand
		charges
	:rtype: tuple
	'''
	if 'flatdemandstructure' not in urdb_response:
		return [], None
	errors, structure = _rate_structure_errors(urdb_response, 'flatdemandstructure')
	months = urdb_response.get('flatdemandmonths')
	if not (isinstance(months, list) and len(months) == 12):
		return errors + ['"flatdemandmonths" must list the "flatdemandstructure" period of each of the 12 months.'], (structure, None)
	if not structure:
		return errors, (structure, None)
	if any(not (_is_number(value) and float(value).is_integer() and 0 <= value < len(structure)) for value in months):
		return errors + [f'"flatdemandmonths" must hold period numbers from 0 to {len(structure) - 1}.'], (structure, None)
	return errors, (structure, [int(value) for value in months])


def _rate_structure_errors(urdb_response, key):
	'''
	Check a rate structure: a list of periods, where each period is a list of tiers and each tier is an object like {"rate": 0.1, "max": 500}. REopt
	bills "rate" plus "adj", and a tier without "max" has no limit

	:param urdb_response: the parsed rate
	:type urdb_response: dict
	:param key: the structure's key, e.g. "energyratestructure"
	:type key: str
	:return: the errors, and the structure if it has none (else None)
	:rtype: tuple
	'''
	structure = urdb_response[key]
	if not (isinstance(structure, list) and len(structure) > 0
		and all(isinstance(period, list) and len(period) > 0 and all(isinstance(tier, dict) for tier in period) for period in structure)):
		return [f'"{key}" must be a list of periods, where each period is a list of one or more tiers like {{"rate": 0.1}}.'], None
	errors = []
	for p, period in enumerate(structure):
		for t, tier in enumerate(period):
			where = f'"{key}" period {p} tier {t + 1}'
			for field in ('rate', 'adj', 'max', 'sell'):
				if field in tier and not _is_number(tier[field]):
					errors.append(f'{where}: "{field}" must be a number, not {json.dumps(tier[field])}.')
			# - The El Paso rate had {"rate": 0.15066, "max": 0} then {"rate": 0}, so REopt billed all of it at $0
			if _is_number(tier.get('max')) and tier['max'] <= 0:
				errors.append(f'{where}: "max" is {tier["max"]}, so nothing can be billed in this tier. A tier\'s "max" must be above 0, and a '
					'period\'s last tier doesn\'t need one.')
	return errors, (None if errors else structure)


def _rate_schedule_errors(urdb_response, key, n_periods):
	'''
	Check a schedule: 12 months of 24 hours, where each hour holds the number of a period in its structure, counting from 0

	:param urdb_response: the parsed rate
	:type urdb_response: dict
	:param key: the schedule's key, e.g. "energyweekdayschedule"
	:type key: str
	:param n_periods: the number of periods in the schedule's structure, or None if the structure has errors (then only the shape is checked)
	:type n_periods: int or None
	:return: the errors, and the schedule as whole numbers if it has none and n_periods is known (else None)
	:rtype: tuple
	'''
	if key not in urdb_response:
		return [f'"{key}" is missing. It must have 12 months of 24 hours each.'], None
	schedule = urdb_response[key]
	if not (isinstance(schedule, list) and len(schedule) == 12 and all(isinstance(month, list) and len(month) == 24 for month in schedule)):
		return [f'"{key}" must have 12 months of 24 hours each.'], None
	if n_periods is None:
		return [], None
	bad_values = sorted({json.dumps(value) for month in schedule for value in month
		if not (_is_number(value) and float(value).is_integer() and 0 <= value < n_periods)})
	if bad_values:
		return [f'"{key}" uses the period(s) {", ".join(bad_values[:8])}, but its periods are numbered 0 to {n_periods - 1}.'], None
	return [], [[int(value) for value in month] for month in schedule]


def _fixed_charge_errors(urdb_response):
	'''
	Check the fixed and minimum charges. REopt ignores "fixedchargefirstmeter" and "mincharge" without one of their units

	:param urdb_response: the parsed rate
	:type urdb_response: dict
	:return: the errors, the fixed charge, and the minimum charge
	:rtype: tuple
	'''
	errors = []
	for key in ('fixedmonthlycharge', 'fixedchargefirstmeter', 'mincharge'):
		if key in urdb_response and not _is_number(urdb_response[key]):
			errors.append(f'"{key}" must be a number, not {json.dumps(urdb_response[key])}.')
	units = ('$/month', '$/day', '$/year')
	fixed_charge = urdb_response.get('fixedmonthlycharge', 0)
	if fixed_charge == 0 and _is_number(urdb_response.get('fixedchargefirstmeter')) and urdb_response['fixedchargefirstmeter'] != 0:
		if urdb_response.get('fixedchargeunits') in units:
			fixed_charge = urdb_response['fixedchargefirstmeter']
		else:
			errors.append('"fixedchargeunits" must be "$/month", "$/day" or "$/year", or REopt ignores "fixedchargefirstmeter".')
	min_charge = 0
	if _is_number(urdb_response.get('mincharge')) and urdb_response['mincharge'] != 0:
		if urdb_response.get('minchargeunits') in units:
			min_charge = urdb_response['mincharge']
		else:
			errors.append('"minchargeunits" must be "$/month", "$/day" or "$/year", or REopt ignores "mincharge".')
	return errors, fixed_charge, min_charge


def _lookback_errors(urdb_response):
	'''
	Check the demand lookback. REopt stops on a rate with both kinds of lookback

	:param urdb_response: the parsed rate
	:type urdb_response: dict
	:return: the errors
	:rtype: list
	'''
	errors = []
	for key in ('lookbackpercent', 'lookbackrange'):
		if key in urdb_response and not _is_number(urdb_response[key]):
			errors.append(f'"{key}" must be a number, not {json.dumps(urdb_response[key])}.')
	lookback_range = urdb_response.get('lookbackrange', 0)
	if _is_number(lookback_range) and not float(lookback_range).is_integer():
		errors.append('"lookbackrange" must be a whole number of months.')
	elif (_is_number(lookback_range) and lookback_range != 0 and isinstance(urdb_response.get('lookbackmonths'), list)
		and len(urdb_response['lookbackmonths']) == 12):
		errors.append('The rate has both "lookbackrange" and 12 "lookbackmonths", which stops REopt. Use only one of them.')
	return errors


def _billed_rates(key, structure, periods):
	'''
	:param key: the structure's key, e.g. "energyratestructure"
	:type key: str
	:param structure: the rate structure, as checked by _rate_structure_errors()
	:type structure: list
	:param periods: the numbers of the periods that a schedule uses
	:type periods: set
	:return: where each tier of those periods is, like '"energyratestructure" period 1 tier 2', and what REopt bills for it: "rate" plus "adj"
	:rtype: list
	'''
	return [(f'"{key}" period {p} tier {t + 1}', tier.get('rate', 0) + tier.get('adj', 0)) for p in sorted(periods) for t, tier in enumerate(structure[p])]


def _tier_limit_warnings(energy, energy_weekday, energy_weekend):
	'''
	Warn when a month's periods have different energy tier limits. REopt takes each month's tier limits from the period of its first weekday hour and
	uses them for the whole month, and it gives a period with fewer tiers copies of its last tier

	:param energy: the energy rate structure, as checked by _rate_structure_errors()
	:type energy: list
	:param energy_weekday: the energy weekday schedule, as checked by _rate_schedule_errors()
	:type energy_weekday: list
	:param energy_weekend: the energy weekend schedule, as checked by _rate_schedule_errors()
	:type energy_weekend: list
	:return: warning messages for the user
	:rtype: list
	'''
	n_tiers = max(len(period) for period in energy)
	if n_tiers == 1:
		return []
	tier_limits = [[tier.get('max') for tier in period + [period[-1]] * (n_tiers - len(period))] for period in energy]
	month_names = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')
	months = [month_names[m] for m in range(12)
		if any(tier_limits[p] != tier_limits[energy_weekday[m][0]] for p in set(energy_weekday[m]) | set(energy_weekend[m]))]
	if not months:
		return []
	return [f'REopt uses the energy tier limits ("max") of each month\'s first weekday hour for the whole month, so the different limits of other '
		f'periods in {", ".join(months)} are ignored.']


def _rate_inputs_by_run(REOPT_INPUTS, MICROGRIDS):
	'''
	:param REOPT_INPUTS: the project's REopt inputs
	:type REOPT_INPUTS: dict or MappingProxyType
	:param MICROGRIDS: the microgrid definitions keyed by microgrid name, with each microgrid's REopt overrides under
		["parameter_overrides"]["reopt_inputs"]
	:type MICROGRIDS: dict or MappingProxyType
	:return: a label and the REopt inputs for the project's rate, which the whole-circuit run and every microgrid without its own rate use, and for
		each microgrid that overrides the rate
	:rtype: list
	'''
	rate_keys = ('urdbResponse', 'energyCost', 'demandCost')
	runs = [('The project\'s rate', dict(REOPT_INPUTS))]
	for mg_name, mg in MICROGRIDS.items():
		overrides = mg.get('parameter_overrides', {}).get('reopt_inputs', {})
		if any(key in overrides for key in rate_keys):
			runs.append((f'Microgrid "{mg_name}"\'s rate', {**REOPT_INPUTS, **overrides}))
	return runs


def _custom_rate_text(inputs):
	'''
	:param inputs: the REopt inputs of one run
	:type inputs: dict
	:return: the run's custom URDB rate as JSON text, or '' if it has none
	:rtype: str
	'''
	rate_text = inputs.get('urdbResponse') or ''
	return rate_text if isinstance(rate_text, str) else json.dumps(rate_text)


#########################################################################################################################################################
# - Whole-run checks: what the GUI's Run checks before it starts microgridup.main(), and what main() checks and warns about for callers that skip the GUI
#########################################################################################################################################################

def raise_for_invalid_run_inputs(data):
	'''
	Run every check on the rest of the inputs, in order: the Outage Location(s), the load CSV, the specific load growth, the additional load shape,
	the historical outages CSV and each REopt run's electricity rate. The GUI's Run calls it after the microgrids are made and their parameter
	overrides are attached, and microgridup.main() calls it through raise_for_invalid_inputs()

	:param data: the run's inputs: BASE_DSS, FAULTED_LINES, LOAD_CSV, LOAD_GROWTH_SPECIFIC, ADDITIONAL_LOADSHAPE_CSV, ADDITIONAL_LOADSHAPE_METER,
		OUTAGE_CSV, REOPT_INPUTS and MICROGRIDS with their parameter overrides attached
	:type data: dict or MappingProxyType
	:rtype: None
	'''
	omd = dssConvert.dssToOmd(data['BASE_DSS'], '', RADIUS=0.0004, write_out=False)
	raise_for_invalid_outage_locations(data['FAULTED_LINES'], omd)
	raise_for_invalid_loads_csv(data['LOAD_CSV'], data['MICROGRIDS'])
	raise_for_invalid_load_growth(data['LOAD_GROWTH_SPECIFIC'], data['LOAD_CSV'])
	if data['ADDITIONAL_LOADSHAPE_CSV']:
		raise_for_invalid_additional_loadshape(data['ADDITIONAL_LOADSHAPE_CSV'], data['ADDITIONAL_LOADSHAPE_METER'], data['LOAD_CSV'])
	if data['OUTAGE_CSV']:
		raise_for_invalid_outages_csv(data['OUTAGE_CSV'])
	raise_for_invalid_rates(data['REOPT_INPUTS'], data['MICROGRIDS'])


def raise_for_invalid_inputs(data):
	'''
	Run every check that stops a run, in order: a microgrid with no loads, the microgrid checks and the checks on the rest of the inputs.
	microgridup.main() calls it when a run starts, for callers that don't go through the GUI, which has already made the same checks

	:param data: the run's inputs as microgridup.main() receives them, with the circuit at BASE_DSS
	:type data: dict or MappingProxyType
	:rtype: None
	'''
	raise_for_microgrids_without_loads(data)
	omd = dssConvert.dssToOmd(data['BASE_DSS'], '', RADIUS=0.0004, write_out=False)
	raise_for_invalid_microgrids(data['MICROGRIDS'], omd)
	raise_for_invalid_run_inputs(data)


def get_input_warnings(data):
	'''
	Return every warning about a run's inputs, in order: loads in no microgrid that would be in a microgrid's island, microgrids with no critical
	loads, the historical outages CSV and each REopt run's electricity rate. microgridup.main() prints them and writes them to logs.log and
	user_warnings.txt when a run starts

	:param data: the run's inputs as microgridup.main() receives them, with the circuit at BASE_DSS
	:type data: dict or MappingProxyType
	:return: warning messages for the user
	:rtype: list
	'''
	omd = dssConvert.dssToOmd(data['BASE_DSS'], '', RADIUS=0.0004, write_out=False)
	warnings = get_outside_load_warnings(data['MICROGRIDS'], omd) + get_no_critical_load_warnings(data['MICROGRIDS'], data['CRITICAL_LOADS'])
	if data['OUTAGE_CSV']:
		warnings += get_outages_csv_warnings(data['OUTAGE_CSV'], omd)
	return warnings + get_rate_warnings(data['REOPT_INPUTS'], data['MICROGRIDS'])


######################################
# - Helpers shared by the checks above
######################################

def _raise_problems(heading, problems):
	'''
	Raise one ValueError that lists the problems under the heading. Do nothing if there are no problems

	:param heading: what the problems are about, e.g. "The load CSV can't be used:"
	:type heading: str
	:param problems: the problems, as messages for the user
	:type problems: list
	:rtype: None
	'''
	if problems:
		msg = heading + '\n' + '\n'.join(f'- {problem}' for problem in problems)
		print(msg)
		raise ValueError(msg)


def _name_list(names, limit=8):
	'''
	:param names: names to show in a message
	:type names: iterable
	:param limit: how many names to show before summarizing the rest
	:type limit: int
	:return: the sorted names in quotes, separated by commas, ending in "and N more" past the limit
	:rtype: str
	'''
	names = sorted(names)
	shown = ', '.join(f'"{name}"' for name in names[:limit])
	if len(names) > limit:
		shown += f' and {len(names) - limit} more'
	return shown


def _line_list(line_numbers, limit=8):
	'''
	:param line_numbers: line numbers of a file to show in a message
	:type line_numbers: iterable
	:param limit: how many line numbers to show before summarizing the rest
	:type limit: int
	:return: "line" or "lines" and then the line numbers separated by commas, ending in "and N more" past the limit
	:rtype: str
	'''
	numbers = [str(number) for number in line_numbers]
	shown = ('line ' if len(numbers) == 1 else 'lines ') + ', '.join(numbers[:limit])
	if len(numbers) > limit:
		shown += f' and {len(numbers) - limit} more'
	return shown


def _is_number(value):
	'''
	:param value: a value from parsed JSON
	:type value: any
	:return: whether the value is a finite number. JSON's true and false don't count, though Python treats bools as ints
	:rtype: bool
	'''
	return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
