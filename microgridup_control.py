import os, csv, math, json, logging, re
from types import MappingProxyType
import pandas as pd
import numpy as np
import networkx as nx
from collections import defaultdict
from plotly import graph_objects, offline, subplots
from scipy.interpolate import RegularGridInterpolator
from omf.solvers.opendss import dssConvert, _getByName, newQstsPlot
import microgridup
import microgridup_validation

# Constants for interpolators.
DIESEL_GENERATOR_SIZES = np.array([20, 30, 40, 60, 75, 100, 125, 135, 150, 175, 200, 230, 250, 300, 350, 400, 500, 600, 750, 1000, 1250, 1500, 1750, 2000, 2250]) # https://www.generatorsource.com/Diesel_Fuel_Consumption.aspx
NATURAL_GAS_GENERATOR_SIZES = np.array([20, 30, 40, 60, 75, 100, 125, 135, 150, 175, 200, 230, 250, 300, 350, 400, 500, 600, 750, 1000]) # https://www.generatorsource.com/Natural_Gas_Fuel_Consumption.aspx
LOAD_LEVELS = np.array([0.25, 0.5, 0.75, 1.0])  # Load as fraction of full load
# Rows correspond to generator sizes, columns correspond to load levels (1/4, 1/2, 3/4, Full Load).
DIESEL_CONSUMPTION_GAL_PER_HOUR = np.array([
    [0.6, 0.9, 1.3, 1.6],
    [1.3, 1.8, 2.4, 2.9],
    [1.6, 2.3, 3.2, 4.0],
    [1.8, 2.9, 3.8, 4.8],
    [2.4, 3.4, 4.6, 6.1],
    [2.6, 4.1, 5.8, 7.4],
    [3.1, 5.0, 7.1, 9.1],
    [3.3, 5.4, 7.6, 9.8],
    [3.6, 5.9, 8.4, 10.9],
    [4.1, 6.8, 9.7, 12.7],
    [4.7, 7.7, 11.0, 14.4],
    [5.3, 8.8, 12.5, 16.6],
    [5.7, 9.5, 13.6, 18.0],
    [6.8, 11.3, 16.1, 21.5],
    [7.9, 13.1, 18.7, 25.1],
    [8.9, 14.9, 21.3, 28.6],
    [11.0, 18.5, 26.4, 35.7],
    [13.2, 22.0, 31.5, 42.8],
    [16.3, 27.4, 39.3, 53.4],
    [21.6, 36.4, 52.1, 71.1],
    [26.9, 45.3, 65.0, 88.8],
    [32.2, 54.3, 77.8, 106.5],
    [37.5, 63.2, 90.7, 124.2],
    [42.8, 72.2, 103.5, 141.9],
    [48.1, 81.1, 116.4, 159.6]
])
CUBIC_FT_TO_MMBTU = 0.001038 # Source: https://www.eia.gov/tools/faqs/faq.php?id=45&t=8
NATURAL_GAS_CONSUMPTION_MMBTU = np.array([
    [157, 188, 247, 289],
    [202, 260, 348, 416],
    [246, 333, 449, 543],
    [334, 479, 652, 798],
    [400, 588, 803, 990],
    [510, 771, 1056, 1308],
    [621, 953, 1308, 1627],
    [665, 1026, 1409, 1754],
    [731, 1135, 1561, 1946],
    [841, 1317, 1813, 2264],
    [952, 1500, 2066, 2583],
    [1084, 1718, 2369, 2965],
    [1172, 1864, 2571, 3220],
    [1393, 2229, 3076, 3857],
    [1614, 2593, 3581, 4495],
    [1834, 2958, 4086, 5132],
    [2276, 3687, 5096, 6407],
    [2717, 4416, 6107, 7681],
    [3379, 5509, 7622, 9593],
    [4482, 7332, 10147, 12780]
]) * CUBIC_FT_TO_MMBTU

def _parse_bus_field(bus_field):
	'''
	Parse a bus-like field which can be:
	  - 'reg0_end.1.2.3'
	  - '[bus1,bus2]'
	  - 'reg0_end'
	  - ['reg0_end.1.2.3'] (list)
	Return a list of bus names (without phase suffixes).
	'''
	if bus_field is None:
		return []
	# If it's a Python list, join into comma string.
	if isinstance(bus_field, (list, tuple)):
		# Take strings in list.
		bus_field = ','.join([str(x) for x in bus_field if x])
	b = str(bus_field).strip()
	# Bracketed list form.
	if b.startswith('[') and b.endswith(']'):
		inner = b[1:-1]
		parts = [p.strip().split('.')[0] for p in inner.split(',') if p.strip()]
		return parts
	# Comma separated.
	if ',' in b:
		parts = [p.strip().split('.')[0] for p in b.split(',') if p.strip()]
		return parts
	# Single entry maybe with phases: split on '.' and return head.
	return [b.split('.')[0]]

def get_first_nodes_of_mgs(dssTree, microgrids, logger=None):
	'''
	Find the "first node" (bus) for each microgrid based on the switch.
	Returns dict mapping microgrid key -> bus name (string).
	'''
	nodes = {}
	for key in microgrids:
		switch = microgrids[key].get('switch')
		# Normalize switch in case it is a list.
		if isinstance(switch, (list, tuple)):
			# Prefer last non-empty, otherwise first.
			switch = next((s for s in reversed(switch) if s), switch[0] if switch else None)
		if switch is None:
			# Fallback to empty string and notify user.
			switch = ''
			if logger:
				logger.warning(f'Error: switch for microgrid {key} could not be found. Fallback to empty string.')
			else:
				print(f'Error: switch for microgrid {key} could not be found. Fallback to empty string.')
		found = None
		# Look for line objects that reference this switch (object names can be 'line', 'line.<name>', or similar).
		for obj in dssTree:
			obj_type = obj.get('object', '')
			# We accept 'line', 'line.name', or object that contains 'line'.
			if 'line' in obj_type:
				# Quick matches: explicit name or embedded suffix.
				if obj.get('name') == switch or obj_type.endswith(f'.{switch}') or f'.{switch}' in obj_type:
					parts = _parse_bus_field(obj.get('bus2') or obj.get('buses') or obj.get('bus'))
					if len(parts) >= 2:
						found = parts[1]
					elif len(parts) == 1:
						found = parts[0]
					if found:
						break
		# It's possible that the switch is a transformer object. 
		if found is None:
			for obj in dssTree:
				obj_type = obj.get('object', '')
				if 'transformer' in obj_type or obj_type.startswith('transformer'):
					buses_field = obj.get('buses') or obj.get('bus') or obj.get('bus2')
					if buses_field:
						bstr = str(buses_field)
						# If switch appears anywhere relevant, pick second bus as "downstream"
						if switch == obj.get('name') or switch in obj_type or switch in bstr:
							parts = _parse_bus_field(buses_field)
							# If transformer buses list, second element is downstream by convention
							if len(parts) >= 2:
								found = parts[1]
							elif len(parts) == 1:
								found = parts[0]
							if found:
								break
		# Fallback: use gen_bus if available (useful for manual mg definitions)
		if found is None:
			found = microgrids[key].get('gen_bus') or ''
			if logger:
				logger.warning(f'Error: no first node found for microgrid {key}. Defaulting to {found}.')
			else:
				print(f'Error: no first node found for microgrid {key}. Defaulting to {found}.')
		nodes[key] = found
	return nodes

def get_all_mg_elements(dssPath, microgrids, omdPath=None, logger=None):
	'''
	Compute all elements in each microgrid by starting from the 'first node' and collecting descendants. If the starting node is missing from the graph, fall back to using explicit microgrid load/gen lists.
	'''
	# Build dssTree and networkx graph.
	if not dssPath:
		dssTree = dssConvert.omdToTree(omdPath)
		G = dssConvert.dss_to_networkx(None, dssTree)
	else:
		dssTree = dssConvert.dssToTree(dssPath)
		G = dssConvert.dss_to_networkx(dssPath)
	first_nodes = get_first_nodes_of_mgs(dssTree, microgrids, logger)
	all_mg_elements = {}
	for key in first_nodes:
		start_node = first_nodes[key]
		N = set()
		# If the start_node exists in graph, use descendants.
		if start_node and start_node in G:
			desc = nx.descendants(G, start_node)
			N = set(desc)
			N.add(start_node)
		else:
			# Fallback: use explicit lists from microgrids (loads and gen_obs_existing and gen_bus).
			if logger:
				logger.warning(f'Error finding first node {start_node} of microgrid {key} using NetworkX. Defaulting to microgrid definition.')
			else:
				print(f'Error finding first node {start_node} of microgrid {key} using NetworkX. Defaulting to microgrid definition.')
			mg = microgrids.get(key, {})
			loads = mg.get('loads', []) or []
			gen_obs_existing = mg.get('gen_obs_existing', []) or []
			gen_bus = [mg.get('gen_bus')] if mg.get('gen_bus') else []
			for item in loads + gen_obs_existing + gen_bus:
				if item:
					N.add(item)
		# Collect transformers that connect inside/outside the set
		transformers = set()
		for obj in dssTree:
			if 'transformer' in obj.get('object',''):
				buses_field = obj.get('buses')
				if not buses_field:
					continue
				buses = _parse_bus_field(buses_field)
				if not buses:
					continue
				b0 = buses[0].split('.')[0]
				b1 = buses[1].split('.')[0] if len(buses) > 1 else None
				# If both ends in N (or one is start_node), mark transformer.
				if ((b0 in N or b0 == start_node) and (b1 in N)) or ((b1 in N or b1 == start_node) and (b0 in N)):
					# obj.get('object') expected like 'transformer.NAME'
					obj_name = obj.get('object')
					if obj_name and '.' in obj_name:
						transformers.add(obj_name.split('.')[1])
					else:
						# Fallback to 'name' field
						if obj.get('name'):
							transformers.add(obj.get('name'))
		# Union node names and transformers.
		all_mg_elements[key] = N.union(transformers)				
	return all_mg_elements

def precompute_mg_element_mapping(dssPath, microgrids, logger):
	'''
	Precomputes a dictionary mapping each element to its microgrid ID.
    '''
	# Get all elements for each microgrid.
	all_mg_elements = get_all_mg_elements(dssPath, microgrids, logger)
	# Create a reverse mapping: element -> microgrid ID.
	element_to_mg = {}
	for mg_id, elements in all_mg_elements.items():
		for element in elements:
			element_to_mg[element] = mg_id
	return element_to_mg

def _get_mg_from_element_name(element_name, element_to_mg):
    '''
    Looks up the microgrid ID for a given element name.
    '''
    return element_to_mg.get(element_name, 'Not_in_MG')

def convert_to_json(all_mg_elements):
	json = {}
	for key in all_mg_elements:
		json[key] = list(all_mg_elements[key])
	return json

def plot_inrush_data(dssPath, microgrids, out_html, outage_start, outage_end, outage_length, logger, vsource_kw_ratings, motor_perc=0.5):
	# Grab all elements by mg. 
	all_mg_elements = get_all_mg_elements(dssPath, microgrids, logger)
	# print('JSON compatible representation of all_mg_elements (for jinja-ing into the circuit map):',convert_to_json(all_mg_elements))
	# logger.warning(f'JSON compatible representation of all_mg_elements (for jinja-ing into the circuit map): {convert_to_json(all_mg_elements)}')
	dssTree = dssConvert.dssToTree(dssPath)

	# Divide up transformers and loads by microgrid.
	table_data = defaultdict(list)
	for key in microgrids:
		table_data['Microgrid ID'].append(key)

		# # of Interruptions --> Number of times gen drops to zero during sim.
		# new_dss_tree, new_batt_loadshape, cumulative_existing_batt_shapes, all_rengen_shapes, total_surplus, all_loads_shapes_kw = do_manual_balance_approach(outage_start, outage_end, microgrids[key], dssTree, logger)
		compute_result = compute_manual_balance(outage_start, outage_end, microgrids[key], dssTree, logger)
		new_dss_tree = apply_manual_balance_to_tree(compute_result, dssTree, logger)   # Need the tree for transformer/load lists below
		new_batt_loadshape = list(compute_result['new_batt_loadshape_sliced'])
		cumulative_existing_batt_shapes = list(compute_result['cumulative_existing_batt_shapes'])
		all_rengen_shapes = list(compute_result['all_rengen_shapes'])
		all_loads_shapes_kw = compute_result['all_loads_shapes_kw']
		# Stole a few lines from plot_manual_balance_approach().
		new_batt_loadshape = list(new_batt_loadshape)
		cumulative_existing_batt_shapes = list(cumulative_existing_batt_shapes)
		gen_and_storage_shape = [x-y for x,y in zip(all_rengen_shapes[outage_start:outage_end],new_batt_loadshape)]
		table_data['# of Interruptions'].append(len(count_interruptions_rengenmg(outage_start, outage_end, outage_length, all_loads_shapes_kw, gen_and_storage_shape)))
		
		# Expected In-rush (kW)
		loads = [obj for obj in new_dss_tree if 'load.' in obj.get('object','') and obj.get('object','').split('.')[1] in microgrids[key]['loads']]
		transformers = [obj for obj in new_dss_tree if 'transformer.' in obj.get('object','') and obj.get('object','').split('.')[1] in all_mg_elements[key]]
		expected_inrush = estimate_inrush(loads + transformers, motor_perc)
		table_data['Expected In-rush (kW)'].append(round(sum(expected_inrush.values()), 3))

		# Expected In-rush (kW) from transformers
		table_data['In-rush (kW) from transformers'].append(round(sum([expected_inrush[ob] for ob in expected_inrush if 'transformer' in ob]), 3))

		# Expected In-rush (kW) from loads
		table_data['Expected In-rush (kW) from loads'].append(round(sum([expected_inrush[ob] for ob in expected_inrush if 'load' in ob]), 3))

		# In-rush as % of total generation
		gen_bus = microgrids[key]['gen_bus']
		all_generation = [ob for ob in new_dss_tree if ob.get('bus1','x.x').split('.')[0] == gen_bus and 'generator' in ob.get('object')]
		total_generation = 0
		for ob in all_generation:
			total_generation += float(ob.get('kw'))
		if total_generation == 0:
			table_data['In-rush as % of total generation'].append(None)
		else:
			table_data['In-rush as % of total generation'].append(round(100*sum(expected_inrush.values())/total_generation, 3))

		# Soft Start load (kW)
		table_data['Soft Start load (kW)'].append(round(gradual_load_pickup(new_dss_tree, loads, motor_perc), 3))

		# Super-cap Sizing
		table_data['Super-cap Sizing ($)'].append(round(super_cap_size(sum(expected_inrush.values())), 3))

		# Total fossil surge. Send total fossil kW power per mg to function, return product after multiplication by surge factor. 
		fossil_gens = [ob for ob in new_dss_tree if ob.get('bus1','x.x').split('.')[0] == gen_bus and 'generator.fossil' in ob.get('object','')]
		table_data['Total fossil surge (kW)'].append(round(calculate_fossil_surge_power(fossil_gens, vsource_kw_ratings, gen_bus), 3))
	
	df = pd.DataFrame(table_data)
	
	table_html = '<h1>In-rush Current Report</h1>' + df.to_html(justify='left').replace('border="1"','border="0"')
	with open(out_html,'w') as outFile:
		outFile.write('<style>* {font-family:sans-serif}</style>' + '\n' + table_html)

def estimate_inrush(list_of_transformers_and_loads, motor_perc=0.5):
	inrush = {}
	for obj in list_of_transformers_and_loads:
		if 'transformer.' in obj.get('object',''):
			inrush[obj.get('object')] = calc_transformer_inrush(obj)
		elif 'load.' in obj.get('object',''):
			inrush[obj.get('object','')] = calc_motor_inrush(obj, motor_perc)
	return inrush

def calculate_fossil_surge_power(fossil_gens, vsource_kw_ratings, gen_bus, surge_factor=2.5):
	# Short burst of power from fossil units. Need a total fossil surge display = 2.5 x total fossil unit power on the microgrid.
	total_kW = 0
	for obj in fossil_gens:
		total_kW += float(obj.get('kw',''))
	for obj in vsource_kw_ratings:
		if gen_bus in obj:
			total_kW += float(vsource_kw_ratings[obj])
	return total_kW * surge_factor

def super_cap_size(magnitude_kw, p=2.5):
	# Assume price of $2.50/Watt. Ignore duration for now. Return price. f(m) = p * magnitude
	p_kw = p * 1000
	return magnitude_kw * p_kw

def gradual_load_pickup(dssTree, loads, motor_perc=0.5):
	# Assume some order of switching on, assume none of the inrushes overlap but the steady state powerflows do, calculate max power during this process.
	# Assume all transformers must be powered when 1 mg is powered.
	loads.sort(key=lambda x:float(x.get('kw')))
	max_load_obj = loads[-1]
	all_transformer_inrush = calc_all_transformer_inrush(dssTree)
	max_load_inrush = calc_motor_inrush(max_load_obj, motor_perc=0.5)
	return max_load_inrush + sum(all_transformer_inrush.values())

def calc_transformer_inrush(dssTransformerDict, default_resistance_transformer='[0.55,0.55]'):
	'''Formula source: https://www.electrical4u.net/transformer/transformer-inrush-current-calculator-with-formula/'''
	# TO DO: figure out if inrushes should be calculated separately for each winding. Current calculates separately but then adds together for one inrush per transformer. 
	# I(peak)(amps) = 1.414 Vm / R(ohms) 
	inrush_amps = 0
	for idx in range(len(dssTransformerDict.get('kvs')[1:-1].split(','))):
		voltage = float(dssTransformerDict.get('kvs')[1:-1].split(',')[idx])
		resistance = float(dssTransformerDict.get('%rs',default_resistance_transformer)[1:-1].split(',')[idx]) * float(dssTransformerDict.get('kvas')[1:-1].split(',')[idx])
		inrush_amps += math.sqrt(2) * voltage / resistance 
	return inrush_amps

def calc_all_transformer_inrush(dssTree):
	transformer_inrushes = {}
	for obj in dssTree:
		if 'transformer.' in obj.get('object',''):
			inrush = calc_transformer_inrush(obj)
			transformer_inrushes[obj.get('object')] = inrush
	return transformer_inrushes

def calc_motor_inrush(dssLoadDict, motor_perc=0.5):
	'''Formula source: http://waterheatertimer.org/calculate-inrush-for-3-phase-motor.html'''
	voltage = float(dssLoadDict.get('kv'))
	power = float(dssLoadDict.get('kw'))
	if dssLoadDict.get('phases') == 3:
		inrush = power / (math.sqrt(3) * voltage)
	else:
		inrush = power / voltage
		# TO DO: make sure single phase motor loads are calculated the same as three phase loads but without root 3 factor.
	# I(amps) = P(w) / (sqrt(3)*E(volts))
	return inrush * motor_perc

def calc_all_motor_inrush(dssTree, motor_perc=0.5):
	motor_inrushes = {}
	for obj in dssTree:
		if 'load.' in obj.get('object',''):
			inrush = calc_motor_inrush(obj, motor_perc)
			motor_inrushes[obj.get('object')] = inrush
	return motor_inrushes

def count_interruptions_rengenmg(outage_start, outage_end, outage_length, all_loads_shapes_kw, gen_and_storage_shape):
	# Count instances where gen_and_storage_shape fail to align with all_loads_shapes_kw.
	demand = all_loads_shapes_kw[outage_start:outage_end]
	supply = gen_and_storage_shape

	interruptions = []
	iS, iE = 0, 0
	idx = 0
	try: #HACK: not working for Eglin.
		while idx < outage_length:
			if supply[idx] < demand[idx]:
				iS = idx
				while idx < outage_length and supply[idx] < demand[idx]:
					idx += 1
				iE = idx
				interruptions.append((iS,iE))
			else:
				idx += 1
	except:
		pass
	return interruptions

def _parse_mult_field(dss_tree, mult_name):
    '''
    Return a numpy 1-D float array parsed from an entry in dss_tree whose object contains mult_name.
    Returns None if not found or parse fails.
    '''
    for ob in dss_tree:
        if ob.get('object') and mult_name in ob.get('object'):
            mult = ob.get('mult')
            if mult is None:
                return None
            # If it's already a list or array
            if isinstance(mult, (list, tuple)):
                try:
                    return np.array([float(x) for x in mult], dtype=float)
                except Exception:
                    return None
            # Otherwise expect a string like '[1,2,3]' or '1,2,3'
            s = str(mult).strip()
            if s.startswith('[') and s.endswith(']'):
                s = s[1:-1]
            if s == '':
                return np.array([], dtype=float)
            try:
                return np.array([float(x) for x in s.split(',')], dtype=float)
            except Exception:
                return None
    return None

def compute_manual_balance(outage_start, outage_end, mg_values, dssTree, logger):
    '''
    Given a dssTree (read-only), compute:
	- per-hour net load (load - rengen)
	- sliced outage battery loadshape (hours outage_start:outage_end)
	- cumulative existing battery shapes (full-year)
	- all_rengen_shapes (full-year)
	- total_surplus list (per outage hour)
	- all_loads_shapes_kw (full-year)
	- batt_obj (list of battery objects found)
	- batt_kwh, batt_kw (scalars)
	- batt_obj_loadshape_names (list) to know which mult objects to replace later
    No mutating dssTree
    Returns a dict (compute_result)
    '''
    # Validate inputs
    gen_bus = mg_values.get('gen_bus')
    if not gen_bus:
        msg = f'Manual balance: missing gen_bus in microgrid values: {mg_values}'
        logger.warning(msg)
        raise ValueError(msg)
    # Collect loads and their yearly shapes
    all_mg_loads = [
        ob for ob in dssTree
        if ob.get('object') and ob.get('object').startswith('load.')
        and ob.get('object').split('.')[1] in mg_values.get('loads', [])
    ]
    if len(all_mg_loads) == 0:
        msg = f'No loads found for microgrid with gen_bus "{gen_bus}". Manual balance cannot proceed.'
        logger.warning(msg)
        raise ValueError(msg)
    load_shapes = []
    for load_ob in all_mg_loads:
        load_shape_name = load_ob.get('yearly')
        arr = _parse_mult_field(dssTree, load_shape_name) if load_shape_name else None
        if arr is None:
            msg = f"Could not parse load yearly shape for load {load_ob.get('object')}"
            logger.warning(msg)
            raise ValueError(msg)
        load_shapes.append(arr)
	# Ensure consistent length
    lengths = {len(a) for a in load_shapes}
    if len(lengths) != 1:
        msg = f'Inconsistent loadshape lengths for microgrid {gen_bus}: lengths {lengths}'
        logger.warning(msg)
        raise ValueError(msg)
    all_loads_shapes_kw = np.sum(np.vstack(load_shapes), axis=0)
    # Second: renewable generation shapes at the gen_bus
    rengen_list = [
        ob for ob in dssTree
        if ob.get('bus1', 'x.x').split('.')[0] == gen_bus
        and (('generator.solar' in ob.get('object', '')) or ('generator.wind' in ob.get('object', '')))
    ]
    if rengen_list:
        rengen_shapes = []
        for g in rengen_list:
            name = g.get('yearly')
            arr = _parse_mult_field(dssTree, name) if name else None
            if arr is None:
                logger.warning(f"Missing/invalid yearly shape for rengen {g.get('object')} at {gen_bus}; treating as zeros.")
                arr = np.zeros_like(all_loads_shapes_kw)
            rengen_shapes.append(arr)
        all_rengen_shapes = np.sum(np.vstack(rengen_shapes), axis=0)
    else:
        all_rengen_shapes = np.zeros_like(all_loads_shapes_kw)
    # Battery objects at gen_bus
    batt_obj = [
        ob for ob in dssTree
        if ob.get('bus1', 'x.x').split('.')[0] == gen_bus and 'storage' in ob.get('object', '')
    ]
    # Deterministic ordering (small to large)
    batt_obj.sort(key=lambda x: float(x.get('kwhrated', 0.0)))
    batt_kws = [float(ob.get('kwrated', 0.0)) for ob in batt_obj]
    batt_kwhs = [float(ob.get('kwhrated', 0.0)) for ob in batt_obj]
    batt_kw = sum(batt_kws)
    batt_kwh = sum(batt_kwhs)
    # Net (load - rengen) (full-year)
    new_batt_loadshape = all_loads_shapes_kw - all_rengen_shapes
    # Validate outage window
    if outage_start < 0 or outage_end > len(new_batt_loadshape) or outage_end <= outage_start:
        msg = f'Invalid outage window [{outage_start},{outage_end}) for loadshape length {len(new_batt_loadshape)}'
        logger.warning(msg)
        raise ValueError(msg)
    new_batt_loadshape_sliced = new_batt_loadshape[outage_start:outage_end].astype(float)
    # Cumulative existing battery shapes
    cumulative_existing_batt_shapes = np.zeros(len(new_batt_loadshape))
    batt_obj_loadshape_names = []
    for ob in batt_obj:
        batt_shape_name = ob.get('yearly')
        arr = _parse_mult_field(dssTree, batt_shape_name) if batt_shape_name else None
        if arr is None:
            logger.warning(f"Missing battery yearly shape for {ob.get('object')}; treating as zeros.")
            arr = np.zeros_like(cumulative_existing_batt_shapes)
        cumulative_existing_batt_shapes = cumulative_existing_batt_shapes + arr
        batt_obj_loadshape_names.append(batt_shape_name)
    # Starting capacity and battery simulation over outage slice
    try:
        starting_capacity = batt_kwh + float(np.sum(cumulative_existing_batt_shapes[:outage_start]))
    except Exception:
        starting_capacity = batt_kwh
    unsupported_load_kwh = 0.0
    total_surplus = []
    # Defensive casts
    try:
        batt_kw = float(batt_kw)
    except Exception:
        batt_kw = 0.0
    try:
        batt_kwh = float(batt_kwh)
    except Exception:
        batt_kwh = 0.0
    battery_available_kwh = float(starting_capacity)
    # Work on a copy of the outage slice
    new_shape_arr = np.array(new_batt_loadshape_sliced, dtype=float)
    for hour_idx in range(len(new_shape_arr)):
        net_kw = new_shape_arr[hour_idx]  # positive => net load; negative => surplus
        surplus_this_hour = 0.0
        max_charge_kwh = batt_kw  # 1-hour timestep
        max_discharge_kwh = min(batt_kw, battery_available_kwh)
        if net_kw < 0:
            pv_surplus_kwh = abs(net_kw)
            available_space_kwh = max(0.0, batt_kwh - battery_available_kwh)
            actual_charge_kwh = min(pv_surplus_kwh, max_charge_kwh, available_space_kwh)
            battery_available_kwh += actual_charge_kwh
            surplus_this_hour = pv_surplus_kwh - actual_charge_kwh
            # positive => charging
            new_shape_arr[hour_idx] = actual_charge_kwh
        else:
            needed_kwh = net_kw
            actual_discharge_kwh = min(needed_kwh, max_discharge_kwh)
            battery_available_kwh -= actual_discharge_kwh
            # negative => discharging (match original sign convention)
            new_shape_arr[hour_idx] = -actual_discharge_kwh
            if needed_kwh > actual_discharge_kwh:
                unsupported_load_kwh += (needed_kwh - actual_discharge_kwh)
        total_surplus.append(surplus_this_hour)
    # Assign back final processed outage slice
    new_batt_loadshape_sliced = new_shape_arr
    # Build and return compute_result dict
    compute_result = {
        'gen_bus': gen_bus,
        'outage_start': int(outage_start),
        'outage_end': int(outage_end),
        'new_batt_loadshape_sliced': new_batt_loadshape_sliced,     # numpy array
        'cumulative_existing_batt_shapes': cumulative_existing_batt_shapes,  # numpy array
        'all_rengen_shapes': all_rengen_shapes,                     # numpy array
        'total_surplus': total_surplus,                             # list
        'all_loads_shapes_kw': all_loads_shapes_kw,                 # numpy array
        'batt_obj': batt_obj,                                       # list of battery objects (dicts)
        'batt_obj_loadshape_names': batt_obj_loadshape_names,       # list of string names
        'batt_kwh': batt_kwh,
        'batt_kw': batt_kw
    }
    return compute_result

def apply_manual_balance_to_tree(compute_result, dssTree, logger):
    '''
    Take the compute_result (from compute_manual_balance) and return a new_dss_tree where battery 'mult' entries for batt_obj_loadshape_names have been patched to include the outage slice values computed in compute_manual_balance().
    Does not modify the input dssTree in place — returns a shallow copy with the modified entries.
    '''
    outage_start = compute_result['outage_start']
    outage_end = compute_result['outage_end']
    new_batt_loadshape_sliced = compute_result['new_batt_loadshape_sliced']
    batt_obj = compute_result['batt_obj']
    batt_obj_loadshape_names = compute_result['batt_obj_loadshape_names']
    batt_kwh = compute_result['batt_kwh']
    # Shallow copy the tree (copy each dict)
    new_dss_tree = [dict(ob) for ob in dssTree]
    if batt_kwh > 0 and len(batt_obj) > 0:
        # Iterate the batt_obj same order as compute()
        for batt_ob, batt_shape_name in zip(batt_obj, batt_obj_loadshape_names):
            try:
                this_kwhr = float(batt_ob.get('kwhrated', 0.0))
                this_kw = float(batt_ob.get('kwrated', 0.0))
                factor = this_kwhr / batt_kwh if batt_kwh > 0 else 0.0
                if this_kw > 0:
                    per_unit_outage_shape = (new_batt_loadshape_sliced * factor) / this_kw
                else:
                    per_unit_outage_shape = np.zeros_like(new_batt_loadshape_sliced)
                # Find the matching mult object index in new_dss_tree
                found_index = None
                for i, t in enumerate(new_dss_tree):
                    if t.get('object') and batt_shape_name in t.get('object'):
                        found_index = i
                        break
                if found_index is None:
                    logger.warning(f"Could not find battery loadshape object for {batt_shape_name}; skipping replacement.")
                    continue
                full_loadshape_arr = _parse_mult_field(dssTree, batt_shape_name)
                if full_loadshape_arr is None:
                    logger.warning(f"Battery loadshape {batt_shape_name} missing or unparsable; skipping.")
                    continue
                full_loadshape_list = full_loadshape_arr.tolist()
                if outage_end > len(full_loadshape_list):
                    logger.warning(f"Outage slice ({outage_start}:{outage_end}) exceeds loadshape length ({len(full_loadshape_list)}). Truncating.")
                    end_idx = min(outage_end, len(full_loadshape_list))
                else:
                    end_idx = outage_end
                new_outage_vals = per_unit_outage_shape.tolist()
                full_loadshape_list[outage_start:end_idx] = new_outage_vals[:end_idx - outage_start]
                # Store back without spaces
                new_dss_tree[found_index]['mult'] = str(full_loadshape_list).replace(' ', '')
            except Exception as e:
                logger.warning(f"Error updating battery loadshape for {batt_ob.get('object', '<unknown>')}: {e}")
    return new_dss_tree

def do_manual_balance_approach(outage_start, outage_end, mg_values, dssTree, logger):
    '''
    Backwards-compatible wrapper: compute -> apply -> return tuple
    Keeps callers unaffected while separating implementation
    '''
    compute_result = compute_manual_balance(outage_start, outage_end, mg_values, dssTree, logger)
    new_dss_tree = apply_manual_balance_to_tree(compute_result, dssTree, logger)
    return (
        new_dss_tree,
        compute_result['new_batt_loadshape_sliced'],
        compute_result['cumulative_existing_batt_shapes'],
        compute_result['all_rengen_shapes'],
        compute_result['total_surplus'],
        compute_result['all_loads_shapes_kw']
    )

def plot_manual_balance_approach(mg_key, year, outage_start, outage_end, outage_length, new_batt_loadshape, cumulative_existing_batt_shapes, all_rengen_shapes, total_surplus, all_loads_shapes_kw):
	# Creates plot of manually constructed battery activity, load, and renewable generation (all cumulative).
	traces = []
	new_batt_loadshape = list(new_batt_loadshape)
	cumulative_existing_batt_shapes = list(cumulative_existing_batt_shapes)
	# Both gen and storage shapes after running battery algorithm.
	rengen_during_outage_post_curtailment = [x-y for x,y in zip(all_rengen_shapes[outage_start:outage_end],total_surplus)]
	gen_and_storage_shape = [x-y for x,y in zip(rengen_during_outage_post_curtailment,new_batt_loadshape)]
	# According to David – storage should be flipped. Discharging is positive. Charging is negative. 
	storage_shapes_for_plot = [-1 * x for x in new_batt_loadshape]

	y_axis_names = ["All loads loadshapes (kW)","All renewable generators loadshapes (kW)","All storage shapes (kW)","Generation and storage (added together)"]
	plotting_variables = [all_loads_shapes_kw[outage_start:outage_end], rengen_during_outage_post_curtailment, storage_shapes_for_plot, gen_and_storage_shape]

	start_time = pd.Timestamp(f"{year}-01-01") + pd.Timedelta(hours=outage_start)
	for idx in range(len(plotting_variables)):
		# Traces for gen, load, storage.
		trace = graph_objects.Scatter(
			x = pd.to_datetime(range(outage_length), unit = 'h', origin = start_time),
			y = plotting_variables[idx],
			showlegend = True,
			name = y_axis_names[idx],
			hoverlabel = dict(namelength = -1)
		)
		traces.append(trace)

	# Plots load traces, gen traces, storage traces.
	layout = graph_objects.Layout(
		title = f'{mg_key} Generation, Load, and Storage in Fully Renewable Microgrid During Outage',
		xaxis = dict(title = 'Date'),
		yaxis = dict(title = "kW"),
		font = dict(
			family="sans-serif",
			color="black"
		)
	)
	fig = graph_objects.Figure(traces, layout)
	out_name = f'{mg_key}_gen_load_storage.plot.html'
	offline.plot(fig, filename=out_name, auto_open=False)
	return out_name

def make_chart_from_csv(csvName, y_list, year, microgrids, chart_name, y_axis_name, outage_start, outage_length, batt_kwh_ratings, fossil_kw_ratings, logger, vsource_kw_ratings={}, rengen_kw_ratings={}, rengen_mgs={}):
	'''
	Plot the contents of a csv generated by newQstsPlot().

	:param csvName: CSV name generated by newQstsPlot(). Prefix is timezcontrol.
	:type csvName: str
	:param y_list: columns in the CSV generated by newQstsPlot() that are to be used as Y-axes
	:type y_list: list
	:param year: year of analysis
	:type year: int
	:param microgrids: all of the microgrid definitions (as defined by microgrid_gen_mgs.py) for the given circuit
	:type microgrids: dict
	:param chart_name: denotes whether plot is for gen, load, or control. Becomes chart title.
	:type chart_name: str
	:param y_axis_name: key for y axis. Different for each of gen/load/control.
	:type y_axis_name: str
	:type outage_start: int
	:param outage_end: hour marking end of outage
	:type outage_end: int
	:param outage_length: length of outage in hours
	:type outage_length: int
	:param batt_kwh_ratings: kwh ratings for each battery in circuit, keyed by microgrid name
	:type batt_kwh_ratings: dict
	:param fossil_kw_ratings: kw ratings for every fossil generator in circuit, keyed by microgrid name
	:type fossil_kw_ratings: dict
	:param vsource_kw_ratings: kw ratings for each microgrid-forming vsource in circuit (aka largest fossil generator in microgrids that were replaced with a vsource of identical size), keyed by microgrid name
	:type vsource_kw_ratings: dict
	:param rengen_kw_ratings: kw ratings for each rengen object in circuit, keyed by microgrid name
	:type rengen_kw_ratings: dict
	:param rengen_mgs: load, rengen, cumulative battery, surplus rengen, and generic loadshapes during outage of microgrid with no fossil generators
	:type rengen_mgs: dict
	:return: no return value
	:rtype: None
	'''
	rengen_proportional_loadshapes = form_rengen_proportional_loadshapes(rengen_mgs, rengen_kw_ratings, microgrids)
	storage_proportional_loadshapes = form_storage_proportional_loadshapes(rengen_mgs, batt_kwh_ratings, microgrids) # Divide batteries into microgrids, find mg total kwh capacities for storage, and form element specific loadshapes.
	fossil_outage_kwh, diesel_dict, natural_gas_dict, fossil_traces, batt_cycles, glc_traces = extract_charting_data(csvName, microgrids, y_list, outage_start, outage_length, fossil_kw_ratings, vsource_kw_ratings, year, rengen_mgs, storage_proportional_loadshapes, batt_kwh_ratings, rengen_proportional_loadshapes, logger)
	if chart_name == 'Generator Output':
		make_fossil_loading_chart(csvName, fossil_traces, fossil_outage_kwh, diesel_dict, natural_gas_dict)
		make_batt_cycle_chart(csvName, batt_cycles)
	ansi_bands = True if chart_name == 'Load Voltage' else False
	make_glc_plots(csvName, chart_name, y_axis_name, glc_traces, outage_start, outage_length, year, ansi_bands)
	return diesel_dict, natural_gas_dict

def form_rengen_proportional_loadshapes(rengen_mgs, rengen_kw_ratings, microgrids):
	'''
	:param rengen_mgs: load, rengen, cumulative battery, surplus rengen, and generic loadshapes during outage of microgrid with no fossil generators
	:type rengen_mgs: dict
	:param rengen_kw_ratings: kw ratings for each rengen object in circuit, keyed by microgrid
	:type rengen_kw_ratings: dict
	:param microgrids: all of the microgrid definitions (as defined by microgrid_gen_mgs.py) for the given circuit
	:type microgrids: dict
	:return: loadshapes for each rengen object organized by microgrid
	:rtype: dict
	'''
	# Define variable to house all rengen element-specific loadshapes organized by microgrid. 
	rengen_proportional_loadshapes = {}
	# Make load loadshape outage patch. Subtract surplus from rengen, add rengen and storage, and compare to load loadshapes. Inequalities = 0, equalities = 1.  
	for mg_key in rengen_mgs:
		# Both gen and storage shapes after running battery algorithm.
		rengen_during_outage_post_curtailment = [x-y for x,y in zip(rengen_mgs[mg_key]["All rengen loadshapes during outage (kw)"],rengen_mgs[mg_key]["Surplus rengen"])]
		gen_and_storage_shape = [round(x-y, 8) for x,y in zip(rengen_during_outage_post_curtailment,rengen_mgs[mg_key]["Cumulative battery loadshape during outage (kw)"])] 
		loadshapes_rounded = [round(num, 8) for num in rengen_mgs[mg_key]["All loads loadshapes during outage (kw)"]]
		generic_mg_loadshape = [1 if x==y else 0 for x,y in zip(gen_and_storage_shape,loadshapes_rounded)] # Bool indicator if generator and storage can support load for each hour during outage.
		rengen_mgs[mg_key]["Generic loadshape (kw)"] = generic_mg_loadshape

		# Divide up cumulative rengen loadshapes during outage proportionally by size to each rengen element. 
		mg_specific_rengen = {} # Define temporary variable to grab the kw ratings of rengen elements specific to current microgrid. 
		mg_proportional_rengen_shapes = {} # Define sub dictionary to be added to collection of all dictionaries.
		for item in rengen_kw_ratings[mg_key]:
			if microgrids[mg_key]['gen_bus'] in item or item in microgrids[mg_key]['gen_obs_existing']:
				mg_specific_rengen[item] = float(rengen_kw_ratings.get(mg_key).get(item))
		total_mg_rengen_capacity_kw = sum(mg_specific_rengen.values())
		for element in mg_specific_rengen:
			factor = mg_specific_rengen[element] / total_mg_rengen_capacity_kw
			# Add each element's loadshapes to mg specific dictionary. 
			mg_proportional_rengen_shapes[element] = [x*factor for x in rengen_mgs[mg_key]["All rengen loadshapes during outage (kw)"]]
		# Finally, add all mg specific loadshapes to all mgs dictionary variable.
		rengen_proportional_loadshapes[mg_key] = mg_proportional_rengen_shapes
	return rengen_proportional_loadshapes

def form_storage_proportional_loadshapes(rengen_mgs, batt_kwh_ratings, microgrids):
	'''
	:param rengen_mgs: load, rengen, cumulative battery, surplus rengen, and generic loadshapes during outage of microgrid with no fossil generators
	:type rengen_mgs: dict
	:param batt_kwh_ratings: kwh ratings for each battery in circuit, keyed by microgrid
	:type batt_kwh_ratings: dict
	:param microgrids: all of the microgrid definitions (as defined by microgrid_gen_mgs.py) for the given circuit
	:type microgrids: dict
	:return: loadshapes for each battery organized by microgrid
	:rtype: dict
	'''
	storage_proportional_loadshapes = {} # Define variable to house all storage element-specific loadshapes organized by microgrid.
	for mg_key in rengen_mgs:
		# Divide up cumulative storage loadshapes during outage proportionally by size to each storage element.
		mg_specific_storage = {} # Define temporary variable to grab the kwh ratings of storage elements specific to current microgrid.
		mg_proportional_storage_shapes = {} # Define sub dictionary to be added to collection of all dictionaries.
		for item in batt_kwh_ratings[mg_key]:
			if microgrids[mg_key]["gen_bus"] in item or item in microgrids[mg_key]['gen_obs_existing']:
				mg_specific_storage[item] = float(batt_kwh_ratings.get(mg_key).get(item))
		total_mg_storage_capacity_kwh = sum(mg_specific_storage.values())
		for element in mg_specific_storage:
			factor = mg_specific_storage[element] / total_mg_storage_capacity_kwh
			# Add each element's loadshapes to mg specific dictionary. 
			mg_proportional_storage_shapes[element] = [x*factor for x in rengen_mgs[mg_key]["Cumulative battery loadshape during outage (kw)"]]
		# Finally, add all mg specific loadshapes to all mgs dictionary variable.
		storage_proportional_loadshapes[mg_key] = mg_proportional_storage_shapes
	return storage_proportional_loadshapes

def get_diesel_interpolator():
	'''Get interpolator once using lazy-initialized singleton pattern.'''
	if not hasattr(get_diesel_interpolator, '_interpolator'):
		get_diesel_interpolator._interpolator = RegularGridInterpolator((DIESEL_GENERATOR_SIZES, LOAD_LEVELS), DIESEL_CONSUMPTION_GAL_PER_HOUR, bounds_error=False, fill_value=None)
	return get_diesel_interpolator._interpolator

def estimate_diesel_consumption(generator_size, load_fraction):
    '''
    Estimate diesel fuel consumption for a given generator size and load fraction using interpolation/extrapolation.
    :param generator_size: Size of the generator in kW.
    :param load_fraction: Load as a decimal fraction (0.0 to 1.0).
    :return: Estimated fuel consumption in gallons per hour.
    '''
    interpolator = get_diesel_interpolator()
    return float(interpolator((generator_size, load_fraction)))

def get_natural_gas_interpolator():
	'''Get interpolator once using lazy-initialized singleton pattern.'''
	if not hasattr(get_natural_gas_interpolator, '_interpolator'):
		get_natural_gas_interpolator._interpolator = RegularGridInterpolator((NATURAL_GAS_GENERATOR_SIZES, LOAD_LEVELS), NATURAL_GAS_CONSUMPTION_MMBTU, bounds_error=False, fill_value=None)
	return get_natural_gas_interpolator._interpolator

def estimate_natural_gas_consumption(generator_size, load_fraction):
    '''
    Estimate natural gas fuel consumption for a given generator size and load fraction using interpolation/extrapolation.
    :param generator_size: Size of the generator in kW.
    :param load_fraction: Load as a decimal fraction (0.0 to 1.0).
    :return: Estimated fuel consumption in mmbtu/hour.
    '''
    interpolator = get_natural_gas_interpolator()
    return float(interpolator((generator_size, load_fraction)))

def remove_trailing_numbers(s):
	'''
	Regex to match a period followed by one or more numbers and optional repetitions of ".<number>". Separates phases at end of string and returns both string sections.
	'''
	match = re.search(r'(\.\d+)+$', s)
	if match:
		phases = match.group(0)  # Extract the matched trailing part
		cleaned_string = s[:match.start()]  # Everything before the match
	else:
		phases = ''  # Nothing to remove
		cleaned_string = s  # Original string remains unchanged
	return cleaned_string, phases

def get_generator_type(monitor, name):
	'''
	Returns prefix to dss object if prefix is solar, wind, fossil, or battery. Otherwise, returns None.
	'''
	parts = name.split('_', 1)
	generator_type = None
	if monitor == 'mongenerator' and parts[0] in {'solar', 'wind', 'fossil', 'battery'}:
		generator_type = parts[0]
	elif monitor == 'monvsource' and parts[0] == 'leadgen':
		generator_type = parts[0]
	return generator_type

def sanitize_series(values, label, logger=None, axis_hint=None, max_ratio_threshold=1e6, hard_clip_abs=None, expected_len=None):
    '''
    Coerce values to numeric list, remove infinities, detect & optionally clip outliers.
    Returns (clean_list, diagnostics_dict).
    - values: sequence-like (pandas Series, list, numpy array)
    - label: string for logging
    - axis_hint: 'PU', 'percent', 'kW' or None (helps choose sane clipping bounds)
    - expected_len: if provided, result will be padded/truncated to this length
    '''
    diagnostics = {'clipped': False, 'nan_count': 0, 'max': None, 'median': None}
    s = pd.Series(values)
    s = pd.to_numeric(s, errors='coerce')
    s = s.replace([np.inf, -np.inf], np.nan)
    diagnostics['nan_count'] = int(s.isna().sum())
    if s.count() == 0:
        if logger:
            logger.warning(f'[sanitize_series] all values non-numeric for {label}; returning zeros.')
        out = [0.0] * (expected_len if expected_len is not None else len(s))
        return out, diagnostics
    med = float(s.median())
    mx = float(s.max())
    diagnostics['max'] = mx
    diagnostics['median'] = med
    # Axis_hint-based clamps
    if axis_hint == 'PU':
        low, high = -0.1, 1.5
    elif axis_hint == 'percent':
        low, high = -10.0, 100.0
    elif axis_hint == 'kW':
        low, high = -500, 3000
    else:
        low, high = -1e15, 1e15
    if hard_clip_abs is not None:
        low, high = -abs(hard_clip_abs), abs(hard_clip_abs)
    # Detect extreme outliers relative to median
    if med != 0 and abs(mx / med) > max_ratio_threshold:
        diagnostics['clipped'] = True
        if logger:
            logger.warning(f'[sanitize_series] {label} max/median > {max_ratio_threshold:.0e}: max={mx}, med={med}. Clipping to bounds.')
    s_clipped = s.clip(lower=low, upper=high)
    s_clean = s_clipped.fillna(0.0).astype(float)
    out = s_clean.tolist()
    # Pad/truncate if expected_len provided
    if expected_len is not None:
        if len(out) < expected_len:
            pad_val = out[-1] if len(out) > 0 else 0.0
            out = out + [pad_val] * (expected_len - len(out))
        elif len(out) > expected_len:
            out = out[:expected_len]
    return out, diagnostics

def extract_charting_data(csvName, microgrids, y_list, outage_start, outage_length, fossil_kw_ratings, vsource_kw_ratings, year, rengen_mgs, storage_proportional_loadshapes, batt_kwh_ratings, rengen_proportional_loadshapes, logger):
	'''
	:param csvName: CSV name generated by newQstsPlot. Prefix is timezcontrol.
	:type csvName: str
	:param microgrids: all of the microgrid definitions (as defined by microgrid_gen_mgs.py) for the given circuit
	:type microgrids: dict
	:param y_list: columns in the CSV generated by newQstsPlot that are to be used as Y-axes
	:type y_list: list
	:param outage_start: hour marking start of outage
	:type outage_start: int
	:param outage_length: length of outage in hours
	:type outage_length: int
	:param fossil_kw_ratings: kw ratings for every fossil generator in circuit, keyed by microgrid
	:type fossil_kw_ratings: dict
	:param vsource_kw_ratings: kw ratings for each microgrid-forming vsource in circuit (aka largest fossil generator in microgrids that were replaced with a vsource of identical size), keyed by microgrid
	:type vsource_kw_ratings: dict
	:param year: year of analysis
	:type year: int
	:param rengen_mgs: load, rengen, cumulative battery, surplus rengen, and generic loadshapes during outage of microgrid with no fossil generators
	:type rengen_mgs: dict
	:param storage_proportional_loadshapes: loadshapes for each battery organized by microgrid
	:type storage_proportional_loadshapes: dict
	:param batt_kwh_ratings: kwh ratings for each battery in circuit, keyed by microgrid
	:type batt_kwh_ratings: dict
	:param rengen_proportional_loadshapes: loadshapes for each rengen object organized by microgrid
	:type rengen_proportional_loadshapes: dict
	:return: total fossil output in kWh, diesel consumption in gallons during the outage (keyed by legend group), natural gas consumption in mmbtu during the outage (keyed by legend group), plotly Scatter objects representing fossil loading percent, number of battery cycles each battery experiences (keyed by battery name and phase), list of 3 traces (plotly Scatter objects) for gen/load/control
	:rtype: int, dict, dict, list, dict, list
	'''
	diesel_dict, natural_gas_dict, fossil_traces = {}, {}, []
	batt_cycles, unreasonable_voltages, glc_traces = {}, {}, []
	fossil_outage_kwh = 0.0
	csv_outage_start, csv_outage_end = 24, 24 + outage_length # newQstsPlot runs from 24h before to 24h after outage
	expected_len = outage_length + 48
	try:
		gen_data = pd.read_csv(csvName)
	except Exception as e:
		msg = f'Failed to read CSV {csvName}: {e}'
		print(msg)
		if logger:
			logger.warning(msg)
		return fossil_outage_kwh, diesel_dict, natural_gas_dict, fossil_traces, batt_cycles, glc_traces
	# Ensure 'Name' exists
	if 'Name' not in gen_data.columns:
		msg = f'CSV {csvName} missing Name column.'
		print(msg)
		if logger:
			logger.warning(msg)
		return fossil_outage_kwh, diesel_dict, natural_gas_dict, fossil_traces, batt_cycles, glc_traces
	# Loop through objects in circuit by monitor
	for monitor_name_phases in gen_data['Name'].unique(): # The 'Name' column in the csvs is usually something like 'monvsource-leadgen_634_air_control.1.2.3'. Break it up to clearly access parts at a time.
        # Separate monitor label.
		try:
			monitor, name_phases = monitor_name_phases.split('-', 1) # E.g. monvsource, leadgen_634_air_control.1.2.3
		except Exception:
			# Fallback: whole string as name_phases
			monitor = 'unknown'
			name_phases = monitor_name_phases
		# Separate phases.
		name, phases = remove_trailing_numbers(name_phases)
		# If object is a generator, name will start with 'solar_', 'wind_', or 'fossil_'. If object is a vsource, name may start with 'leadgen_', or it may not in which case it is merely a vsource.
		generator_type = get_generator_type(monitor, name) # E.g. leadgen, 634_air_control. Returns None if object is not a generator. 

		# Set appropriate legend group based on microgrid.
		legend_group = 'Not_in_MG'
		for key in microgrids:
			try:
				if microgrids[key]['gen_bus'] in name or name in microgrids[key]['loads'] or name in microgrids[key].get('gen_obs_existing', ()):
					legend_group = key
					break
			except Exception:
				msg = f'Unable to set a legend group for {name}. Skipping.'
				print(msg)
				if logger:
					logger.warning(f'Unable to set a legend group for {name}. Skipping.')
				continue

		# Loop through phase columns
		for y_name in y_list:
			# Select rows matching this monitor
			this_series_df = gen_data[gen_data['Name'] == monitor_name_phases]

			# Defensive numeric conversion -> numpy array of expected_len
			raw_series = this_series_df[y_name] if y_name in this_series_df.columns else pd.Series(dtype='float64')
			numeric = pd.to_numeric(raw_series, errors='coerce').replace([np.inf, -np.inf], np.nan) # Replace +/-infinity with NaN
			if logger:
				logger.debug(f'[extract_charting_data] {csvName}:{monitor_name_phases}:{y_name} coercion -> count={int(numeric.count())} nan={int(numeric.isna().sum())}')
			# Fill NaN with 0 for plotting, then make numpy array
			numeric_filled = numeric.fillna(0.0).astype(float)
			vals = numeric_filled.to_numpy(dtype=float)
			# Ensure length matches expected_len (pad/truncate)
			if len(vals) != expected_len:
				msg = f'[extract_charting_data] Series {csvName}:{monitor_name_phases}:{y_name} length {len(vals)} != expected {expected_len}. Padding/truncating.'
				print(msg)
				if logger:
					logger.warning(msg)
				if len(vals) < expected_len:
					pad_val = vals[-1] if len(vals) > 0 else 0.0
					vals = np.concatenate([vals, np.full(expected_len - len(vals), pad_val)])
				else:
					vals = vals[:expected_len]

			# Amass data for fuel consumption chart.
			if generator_type in {'fossil', 'leadgen'}:
				try:
					phase_slice = vals[csv_outage_start:csv_outage_end]
				except Exception:
					phase_slice = vals[max(0, csv_outage_start):min(len(vals), csv_outage_end)]
				fossil_outage_phase_kwh = float(np.sum(np.abs(phase_slice)))
				fossil_outage_kwh += fossil_outage_phase_kwh # Fossil generation is typically negative. Vsource gen is positive.
				fossil_kw_rating = None
				if generator_type == 'fossil':
					fossil_kw_rating = fossil_kw_ratings.get(legend_group, {}).get(name)
				else:
					fossil_kw_rating = vsource_kw_ratings.get(legend_group, {}).get(name)
				fossil_kw_rating = float(fossil_kw_rating) if fossil_kw_rating is not None else None
				if fossil_kw_rating is None or fossil_kw_rating == 0:
					msg = f'[extract_charting_data] Missing/invalid kw rating for generator "{name}" in group "{legend_group}". Skipping fuel/percent calculations.'
					print(msg)
					if logger:
						logger.warning(msg)
				else:
					# Average loading across outage
					fossil_loading_average_decimal = fossil_outage_phase_kwh / (fossil_kw_rating * max(1, outage_length))
					if not np.isfinite(fossil_loading_average_decimal):
						fossil_loading_average_decimal = 0.0
					if fossil_loading_average_decimal > 1.0:
						if logger:
							logger.warning(f'[extract_charting_data] fossil_loading_average >1 ({fossil_loading_average_decimal}) for {name}. Clipping to 1.')
						fossil_loading_average_decimal = 1.0 # TODO: Fossil loading average should never exceed 100%. Rounding fossil loading averages to 1 is only a short term fix.
					elif 0 < fossil_loading_average_decimal < 0.25:
						if logger:
							logger.debug(f'[extract_charting_data] fossil_loading_average <0.25 ({fossil_loading_average_decimal}) for {name}. Flooring to 0.25.')
						fossil_loading_average_decimal = 0.25 # TODO: Fossil loading average should not be below 25%. https://www.cat.com/en_US/by-industry/electric-power/Articles/White-papers/the-impact-of-generator-set-underloading.html
					# Calculate diesel_consumption_per_hour and natural_gas_consumption_mmbtu_per_hour using interpolation.
					diesel_consumption_gal_per_hour = estimate_diesel_consumption(fossil_kw_rating, fossil_loading_average_decimal) if fossil_loading_average_decimal else 0.0
					diesel_consumption_outage_gal = diesel_consumption_gal_per_hour * outage_length
					natural_gas_consumption_mmbtu_per_hour = estimate_natural_gas_consumption(fossil_kw_rating, fossil_loading_average_decimal) if fossil_loading_average_decimal else 0.0
					natural_gas_consumption_outage_mmbtu = natural_gas_consumption_mmbtu_per_hour * outage_length
					diesel_dict[legend_group] = diesel_dict.get(legend_group, 0.0) + diesel_consumption_outage_gal
					natural_gas_dict[legend_group] = natural_gas_dict.get(legend_group, 0.0) + natural_gas_consumption_outage_mmbtu
					# Make fossil loading percentages traces.
					if generator_type == 'fossil':
						pct = (vals / fossil_kw_rating) * -100.0
					else:
						pct = (vals / fossil_kw_rating) * 100.0
					pct_list, pct_diag = sanitize_series(pct, label=f"{csvName}:pct:{monitor_name_phases}:{y_name}", logger=logger, axis_hint='percent', expected_len=expected_len)
					graph_start_time = pd.Timestamp(f'{year}-01-01') + pd.Timedelta(hours=outage_start-24)
					fossil_trace = graph_objects.Scatter(
						x = pd.to_datetime(range(expected_len), unit='h', origin=graph_start_time),
						y = pct_list,
						legendgroup=legend_group,
						legendgrouptitle_text=legend_group,
						showlegend=True,
						name=monitor_name_phases + '_' + y_name,
						hoverlabel = dict(namelength = -1)
					)
					fossil_traces.append(fossil_trace)

			# If battery_, count battery cycles.
			if (monitor == 'mongenerator' and generator_type == 'battery'):
				try:
					if rengen_mgs and legend_group in rengen_mgs:
						curr_series_list = list(vals)
						outage_portion = storage_proportional_loadshapes[legend_group].get(name_phases, [0.0]*outage_length)
						all_batt_loadshapes = curr_series_list[:csv_outage_start] + outage_portion + curr_series_list[csv_outage_end:]
						batt_kwh_input_output = sum([abs(x) for x in all_batt_loadshapes])
					else:
						batt_kwh_input_output = float(np.sum(np.abs(vals)))
					mg_batt_dict = batt_kwh_ratings.get(legend_group, {})
					batt_kwh_rating = mg_batt_dict.get(name_phases)
					if batt_kwh_rating is None:
						msg = f'[extract_charting_data] missing batt kWh rating for "{name_phases}" in group "{legend_group}". Skipping cycle calc.'
						print(msg)
						if logger:
							logger.warning(msg)
					else:
						batt_kwh_rating = float(batt_kwh_rating)
						if batt_kwh_rating == 0:
							msg = f'[extract_charting_data] zero batt kWh rating for "{name_phases}" in group "{legend_group}". Skipping cycle calc.'
							print(msg)
							if logger:
								logger.warning(msg)
						else:
							cycles = batt_kwh_input_output / (2.0 * batt_kwh_rating)
							batt_cycles[f"{monitor_name_phases}_{y_name}"] = cycles
				except Exception as e:
					msg = f'[extract_charting_data] error computing battery cycles for {monitor_name_phases}:{y_name}: {e}'
					print(msg)
					if logger:
						logger.warning(msg)

			# Flag loads that don't fall within ansi bands.
			if '_load' in csvName:
				svals = np.array(vals, dtype=float)
				if (svals > 1.1).any() or (svals < 0.9).any():
					unreasonable_voltages[f'{monitor_name_phases}_{y_name}'] = monitor_name_phases

			# Traces for gen, load, control. 
			if 'mongenerator' in monitor_name_phases:
				y_axis_arr = (-1.0) * vals  # Original code inverted generation
			else:
				y_axis_arr = vals
			# Splice over the outage portions if manual balance approach was used (rengen only circuit).
			if rengen_mgs and legend_group in rengen_mgs:
				pre = list(y_axis_arr[:csv_outage_start])
				post = list(y_axis_arr[csv_outage_end:])
				splice = []
				if (monitor == 'mongenerator' and generator_type in ('wind','solar')):
					splice = rengen_proportional_loadshapes.get(legend_group, {}).get(name_phases, [])
				elif monitor == 'mongenerator' and generator_type == 'battery':
					splice = storage_proportional_loadshapes.get(legend_group, {}).get(name_phases, [])
				elif monitor == 'monload':
					splice = rengen_mgs.get(legend_group, {}).get('Generic loadshape (kw)', [])
				# Normalize splice length to outage_length
				if len(splice) != outage_length:
					msg = f'[extract_charting_data] splice length for {monitor_name_phases} is {len(splice)} (expected {outage_length}). Adjusting.'
					print(msg)
					if logger:
						logger.warning(msg)
					if len(splice) < outage_length:
						pad_val = splice[-1] if len(splice) > 0 else 0.0
						splice = splice + [pad_val] * (outage_length - len(splice))
					else:
						splice = splice[:outage_length]
				y_axis = pre + splice + post
			else:
				y_axis = list(y_axis_arr)

			# Final sanitization for plotting
			axis_hint = 'kW' if ('kW' in y_name or 'Gen' in y_name) else ('PU' if ('PU' in y_name or 'V' in y_name) else None)
			y_axis_sanitized, diag = sanitize_series(y_axis, label=f'{csvName}:{monitor_name_phases}:{y_name}', logger=logger, axis_hint=axis_hint, expected_len=expected_len)
			# Prepare legend group label for manual-balance
			plot_legend_group = f'{legend_group} – Manual Balance Approach used during outage' if (rengen_mgs and legend_group in rengen_mgs) else legend_group
			# Final trace - only add if we have finite numeric data
			try:
				graph_start_time = pd.Timestamp(f'{year}-01-01') + pd.Timedelta(hours=outage_start-24)
				trace = graph_objects.Scatter(
					x = pd.to_datetime(range(expected_len), unit='h', origin=graph_start_time),
					y = y_axis_sanitized,
					legendgroup = plot_legend_group,
					legendgrouptitle_text = plot_legend_group,
					showlegend = True,
					name = monitor_name_phases + '_' + y_name + (('_[voltage_violation]') if (monitor_name_phases + '_' + y_name) in unreasonable_voltages else ''),
                    hoverlabel = dict(namelength = -1)
				)
				glc_traces.append(trace)
			except Exception as e:
				msg = f'[extract_charting_data] failed to create trace for {monitor_name_phases}:{y_name}: {e}'
				print(msg)
				if logger:
					logger.warning(msg)
	return fossil_outage_kwh, diesel_dict, natural_gas_dict, fossil_traces, batt_cycles, glc_traces

def make_fossil_loading_chart(csvName, fossil_traces, fossil_outage_kwh, diesel_dict, natural_gas_dict):
	'''
	:param csvName: CSV name generated by newQstsPlot. Prefix is timezcontrol.
	:type csvName: str
	:param fossil_traces: ploty Scatter objects representing fossil loading percent
	:type fossil_traces: list
	:param diesel_dict: stores diesel consumption in gallons during the outage, keyed by legend group
	:type diesel_dict: dict
	:param natural_gas_dict: stores diesel consumption in mmbtu during the outage, keyed by legend group
	:type natural_gas_dict: dict
	:return: don't return anything. Instead, plot figure to file.
	:rtype: None 
	'''
	# Make fossil genset loading plot. 
	new_layout = graph_objects.Layout(
		title = f"Fossil Genset Loading Percentage ({csvName})",
		xaxis = dict(title = 'Date'),
		yaxis = dict(title = 'Fossil Percent Loading'),
		font = dict(family="sans-serif", color="black"))
	fossil_fig = graph_objects.Figure(fossil_traces, new_layout)
	offline.plot(fossil_fig, filename=f'{csvName}_fossil_loading.plot.html', auto_open=False)
	# Calculate total fossil genset consumption and make fossil fuel consumption chart. 
	fossil_outage_kwh = "{:e}".format(fossil_outage_kwh)
	total_gal_diesel = "{:e}".format(sum(diesel_dict.values()))
	total_mmbtu_gas = "{:e}".format(sum(natural_gas_dict.values()))
	# Make fossil fuel consumption chart. 
	diesel_dict = dict(sorted(diesel_dict.items()))
	natural_gas_dict = dict(sorted(natural_gas_dict.items()))
	# Construct plot.
	fig = subplots.make_subplots(shared_xaxes=True, specs=[[{"secondary_y": True}]])
	fig.add_trace(graph_objects.Bar(x = list(diesel_dict.keys()), y = list(diesel_dict.values()), name="Diesel"), secondary_y=False)
	fig.add_trace(graph_objects.Bar(x = list(natural_gas_dict.keys()), y = list(natural_gas_dict.values()), name="Gas"), secondary_y=True)
	fig.update_layout(
		title_text = f"Diesel and Natural Gas Equivalent Consumption During Outage By Microgrid<br><sup>Total Consumption in Gallons of Diesel = {total_gal_diesel} || Total Consumption in MMBTU Natural Gas = {total_mmbtu_gas} || Total Output in kWh = {fossil_outage_kwh}</sup>",
		font = dict(family="sans-serif", color="black"),
		legend=dict(orientation='h'))
	fig.update_xaxes(title_text="Microgrid")
	fig.update_yaxes(title_text="Gallons of Diesel Equivalent Consumed During Outage", secondary_y=False)
	fig.update_yaxes(title_text="MMBTU of Natural Gas Equivalent Consumed During Outage", secondary_y=True)
	offline.plot(fig, filename=f'{csvName}_fuel_consumption.plot.html', auto_open=False)

def make_batt_cycle_chart(csvName, batt_cycles):
	'''
	:param csvName: CSV name generated by newQstsPlot. Prefix is timezcontrol.
	:type csvName: str
	:param batt_cycles: stores number of battery cycles each battery experiences, keyed by battery name and phase
	:type batt_cycles: dict
	:return: don't return anything. Instead, plot figure to file.
	:rtype: None 
	'''
	# Make battery cycles bar chart.
	batt_cycles = dict(sorted(batt_cycles.items()))
	new_trace = graph_objects.Bar(
		x = list(batt_cycles.keys()), 
		y = list(batt_cycles.values()))
	new_layout = graph_objects.Layout(
		title = "Battery Cycles During Analysis Period",
		xaxis = dict(title = 'Battery'),
		yaxis = dict(title = 'Cycles'),
		font = dict(family="sans-serif", color="black"))
	new_fig = graph_objects.Figure(new_trace, new_layout)
	offline.plot(new_fig, filename=f'{csvName}_battery_cycles.plot.html', auto_open=False)

def make_glc_plots(csvName, chart_name, y_axis_name, glc_traces, outage_start, outage_length, year, ansi_bands):
	'''
	:param csvName: CSV name generated by newQstsPlot. Prefix is timezcontrol.
	:type csvName: str
	:param chart_name: denotes whether plot is for gen, load, or control. Becomes chart title.
	:type chart_name: str
	:param y_axis_name: key for y axis. Different for each of gen/load/control.
	:type y_axis_name: str
	:param glc_traces: list of 3 traces (plotly Scatter objects) for gen/load/control
	:type glc_traces: list
	:param outage_start: hour marking start of outage
	:type outage_start: int
	:param outage_length: length of outage in hours
	:type outage_length: int
	:param year: year of analysis
	:type year: int
	:param ansi_bands: in load plot, red dotted lines denoting 0.9 - 1.1 of load voltage
	:type ansi_bands: bool
	:return: don't return anything. Instead, plot figure to file.
	:rtype: None
	'''
	# Plots for gen, load, control.
	layout = graph_objects.Layout(
		title = f'{chart_name} <br><sup>Dotted black lines indicate outage start and end times</sup>',
		xaxis = dict(title = 'Date'),
		yaxis = dict(title = y_axis_name),
		font = dict(family="sans-serif", color="black"))
	fig = graph_objects.Figure(glc_traces, layout)
	if ansi_bands == True:
		line_style = {'color':'Red', 'width':3, 'dash':'dashdot'}
		fig.add_hline(y=0.9, line=line_style)
		fig.add_hline(y=1.1, line=line_style)
	# Add outage start and end markers.
	outage_line_style = {'color':'Black', 'width':1, 'dash':'dot'}
	start_time = pd.Timestamp(f"{year}-01-01") + pd.Timedelta(hours=outage_start)
	end_time = pd.Timestamp(f"{year}-01-01") + pd.Timedelta(hours=outage_start) + pd.Timedelta(hours=outage_length)
	fig.add_vline(x=start_time, line=outage_line_style)
	fig.add_vline(x=end_time, line=outage_line_style)
	offline.plot(fig, filename=f'{csvName}.plot.html', auto_open=False)

def calculate_land_for_diesel_storage(diesel_gal):
	'''
	Calculate the land area needed to store a volume of diesel. 
	See https://docs.google.com/document/d/1uQEOsu7S90QVoh5zDcpvNW6v4vnIDuTj6mxBBgpHQVs/edit?usp=sharing for research and assumptions.

	:param diesel_gal: volume of diesel in gallons
	:type: float
	:return: area in square feet of footprint of tanks necessary to store specified volume of diesel (does not include buffer zone)
	:rtype: float
	'''
	diesel_gal_per_tank = 18175 
	area_square_feet_per_tank = 320
	area_square_feet = math.ceil(diesel_gal / diesel_gal_per_tank) * area_square_feet_per_tank
	return area_square_feet

def dp(volume, tank_volumes, tank_areas, memo={}):
	'''
	Dynamic programming helper function used by calculate_land_for_diesel_storage_optimized. Top-down (recursive) dynamic programming with memoization.
	'''
	if volume == 0:
		return 0 # Base case: reached if a storage tank was picked and there was no leftover volume to store, so no area should be added
	if volume in memo:
		return memo[volume]
	min_area = float('inf') # Will store minimum area after attempting to select all tank sizes. If remaining volume is smaller than all tank sizes, min_area will be set to area of smallest tank
	for idx, tank_volume in enumerate(tank_volumes):
		if tank_volume <= volume:
			min_area_take_tank = tank_areas[idx] + dp(volume - tank_volume, tank_volumes, tank_areas, memo)
			min_area = min(min_area, min_area_take_tank) # Compare to not taking tank
	if min_area == float('inf'):
		min_area = tank_areas[0] # Store remaining volume that is smaller than all tank volumes with smallest-volumed tank
	memo[volume] = min_area
	return min_area

def calculate_land_for_diesel_storage_optimized(diesel_gal):
	'''
	Calculate the land area needed to store a volume of diesel. 
	See https://docs.google.com/document/d/1uQEOsu7S90QVoh5zDcpvNW6v4vnIDuTj6mxBBgpHQVs/edit?usp=sharing for research and assumptions. Tank sizes taken from: https://western-global.com/us/products/transtank-pro/. 

	:param diesel_gal: volume of diesel in gallons
	:type: float
	'''
	tank_volumes = [3223, 8189, 18175]
	tank_volumes.sort() # Other tank volumes can be substituted and the volumes will need to be in ascending order
	tank_areas = [118 * 96 / 144, 239 * 96 / 144, 480 * 96 / 144] # [78.66666667, 159.33333333, 320]
	area_square_feet = dp(diesel_gal, tank_volumes, tank_areas)
	return area_square_feet

def calculate_land_for_natural_gas_storage(natural_gas_cubic_feet, cng_or_lng='cng'):
	'''
	Calculate the land area needed to store a volume of natural gas. 
	See https://docs.google.com/document/d/1uQEOsu7S90QVoh5zDcpvNW6v4vnIDuTj6mxBBgpHQVs/edit?usp=sharing for research and assumptions.

	:param natural_gas_cubic_feet: volume of natural gas in cubic feet 
	:type: float
	:param cng_or_lng: compressed natural gas (CNG) or liquefied natural gas (LNG). Can be 'cng' or 'lng'
	:type: str
	:return: area in square feet of footprint of tanks necessary to store specified volume of natural gas of specified type (does not include buffer zone)
	:rtype: float
	'''
	if cng_or_lng == 'cng':
		natural_gas_cubic_feet_per_tank = 33840
		area_square_feet_per_tank = 84
		area_square_feet = math.ceil(natural_gas_cubic_feet / natural_gas_cubic_feet_per_tank) * area_square_feet_per_tank
	elif cng_or_lng == 'lng':
		natural_gas_cubic_feet_per_tank = 1245000
		area_square_feet_per_tank = 467.5
		area_square_feet = math.ceil(natural_gas_cubic_feet / natural_gas_cubic_feet_per_tank) * area_square_feet_per_tank
	return area_square_feet

def estimate_fuel_land_use(diesel_dict, natural_gas_dict):
	diesel_totals = {}
	for mg in diesel_dict:
		diesel_totals[mg] = calculate_land_for_diesel_storage_optimized(diesel_dict[mg])
	natural_gas_totals = {}
	for mg in natural_gas_dict:
		natural_gas_totals[mg] = calculate_land_for_natural_gas_storage(natural_gas_dict[mg] / 0.001038, cng_or_lng='cng') # mmbtu to cubic feet: https://www.eia.gov/tools/faqs/faq.php?id=45&t=8
	return {
		'diesel_storage': diesel_totals,
		'natural_gas_storage': natural_gas_totals
	}

def estimate_fossil_generation_land_use(capacity_mw, diesel_or_natural_gas='diesel'):
	land_acres = None
	if diesel_or_natural_gas == 'diesel':
		land_acres = capacity_mw * 0.08710855
	elif diesel_or_natural_gas == 'natural_gas':
		land_acres = capacity_mw * 0.1164
	return land_acres

def estimate_rengen_land_use(capacity_mw, solar_or_wind='solar'):
	land_acres = None
	if solar_or_wind == 'solar':
		land_acres = capacity_mw * 8.1
	elif solar_or_wind == 'wind':
		land_acres = capacity_mw * 60
	return land_acres

def estimate_battery_land_use(capacity_mwh):
	return capacity_mwh * 0.3

def estimate_generation_land_use(fossil_kw_ratings, vsource_kw_ratings, rengen_kw_ratings, battery_kwh_ratings):
	'''
	Estimate total land used by all generation objects in circuit.

	:param fossil_kw_ratings: kw ratings for every fossil generator in circuit, keyed by microgrid
	:type fossil_kw_ratings: dict
	:param rengen_kw_ratings: kw ratings for each rengen object in circuit, keyed by microgrid
	:type rengen_kw_ratings: dict
	:param battery_kwh_ratings: kwh ratings for each battery in circuit, keyed by microgrid
	:type battery_kwh_ratings: dict
	:return: acres needed to store all generation objects in circuit
	:rtype: float
	'''
	diesel_totals, natural_gas_totals = {}, {}
	for mg in fossil_kw_ratings:
		diesel_mg = 0
		natural_gas_mg = 0
		for gen in fossil_kw_ratings[mg]:
			kw_rating = float(fossil_kw_ratings[mg][gen])
			diesel_mg += estimate_fossil_generation_land_use(kw_rating / 1000, diesel_or_natural_gas='diesel')
			natural_gas_mg += estimate_fossil_generation_land_use(kw_rating / 1000, diesel_or_natural_gas='natural_gas')
		diesel_totals[mg] = diesel_mg
		natural_gas_totals[mg] = natural_gas_mg
	for mg in vsource_kw_ratings:
		diesel_mg = 0
		natural_gas_mg = 0
		for vsource in vsource_kw_ratings[mg]:
			kw_rating = float(vsource_kw_ratings[mg][vsource])
			diesel_mg += estimate_fossil_generation_land_use(kw_rating / 1000, diesel_or_natural_gas='diesel')
			natural_gas_mg += estimate_fossil_generation_land_use(kw_rating / 1000, diesel_or_natural_gas='natural_gas')
		diesel_totals[mg] = diesel_totals.get(mg, 0) + diesel_mg
		natural_gas_totals[mg] = natural_gas_totals.get(mg, 0) + diesel_mg
	
	solar_totals, wind_totals = {}, {}
	for mg in rengen_kw_ratings:
		solar_mg = 0
		wind_mg = 0
		for gen in rengen_kw_ratings[mg]:
			kw_rating = float(rengen_kw_ratings[mg][gen])
			gen_type, name = gen.split('_', 1)
			if gen_type == 'solar':
				solar_mg += estimate_rengen_land_use(kw_rating / 1000, gen_type)
			elif gen_type == 'wind':
				solar_mg += estimate_rengen_land_use(kw_rating / 1000, gen_type)
			else:
				raise ValueError(f'Unrecognized generation type: {gen_type}')
		solar_totals[mg] = solar_mg
		wind_totals[mg] = wind_mg
		
	battery_totals = {}
	for mg in battery_kwh_ratings:
		battery_mg = 0
		for battery in battery_kwh_ratings[mg]:
			kwh_rating = float(battery_kwh_ratings[mg][battery])
			battery_mg += estimate_battery_land_use(kwh_rating)
		battery_totals[mg] = battery_mg

	return {
		'diesel_generation': diesel_totals,
		'natural_gas_generation': natural_gas_totals,
		'solar': solar_totals,
		'wind': wind_totals,
		'battery': battery_totals
	}

def make_sizing_chart(microgrids, out_html, fossil_kw_ratings, vsource_kw_ratings, rengen_kw_ratings, batt_kwh_ratings, diesel_dict, natural_gas_dict):
	'''
	Chart format: 
	Microgrid ID					 mg0 mg1 mg2 mg3 total
	Fossil generation size (kW)
	Diesel fuel (gallons)
	Natural gas fuel (mmbtu)
	Solar generation size (kW)
	Wind generation size (kW)
	Battery storage size (kWh)
	'''
	# Get rengen totals by mg and type.
	rengen_totals = defaultdict(lambda: {'solar': 0, 'wind': 0})
	for mg, generators in rengen_kw_ratings.items():
		for generator, kw_rating in generators.items():
			gen_type = generator.split('_')[0]
			if gen_type == 'solar':
				rengen_totals[mg]['solar'] += float(kw_rating)
			elif gen_type == 'wind':
				rengen_totals[mg]['wind'] += float(kw_rating)
	# Get fossil totals by mg.
	fossil_totals = {mg: sum(float(kw) for kw in generators.values()) 
				  for mg, generators in fossil_kw_ratings.items()}
	for mg, vsources in vsource_kw_ratings.items():
		fossil_totals[mg] = fossil_totals.get(mg, 0) + sum(float(kw) for kw in vsources.values()) # I don't think there can be multiple vsources but this is just in case there are.
	# Get battery totals by mg.
	battery_totals = {mg: sum(float(kwh) for kwh in batteries.values()) 
				   for mg, batteries in batt_kwh_ratings.items()}
	rows = [
		'Fossil generation size (kW)',
		'Diesel fuel (gallons)',
		'Natural gas fuel (mmbtu)',
		'Solar generation size (kW)',
		'Wind generation size (kW)',
		'Battery storage size (kWh)'
	]
	df = pd.DataFrame(index=rows)
	for mg in microgrids:
		df[mg] = [
			fossil_totals.get(mg, 0),
			diesel_dict.get(mg, 0),
			natural_gas_dict.get(mg, 0),
			rengen_totals.get(mg, {}).get('solar', 0),
			rengen_totals.get(mg, {}).get('wind', 0),
			battery_totals.get(mg, 0)
		]
	# Add totals to final column
	df['Total'] = df.sum(axis=1)
	df = df.round(2)
	df = df.rename_axis('Microgrid ID').reset_index()
	table_html = '<h1>Generation Sizes and Fuel Amounts</h1>' + df.to_html(justify='left').replace('border="1"','border="0"')
	with open(out_html,'w') as outFile:
		outFile.write('<style>* {font-family:sans-serif}</style>' + '\n' + table_html)

def make_land_use_chart(microgrids, out_html, fossil_kw_ratings, vsource_kw_ratings, rengen_kw_ratings, batt_kwh_ratings, diesel_dict, natural_gas_dict):
	'''
	Chart format: 

	Microgrid ID                  			 mg0 mg1 mg2 mg3 total
	Diesel generation land use (acres)
	Diesel fuel storage land use (acres)
	Natural gas generation land use (acres)
	Natural gas fuel storage land use (acres)
	Solar generation land use (acres)
	Wind generation land use (acres)
	Battery storage land use (acres)
	Total (acres)
	'''
	# Estimate land use by fuel storage.
	fuel_land_use_acres = estimate_fuel_land_use(diesel_dict, natural_gas_dict)
	# Estimate land use by generation and storage.
	generation_land_use_acres = estimate_generation_land_use(fossil_kw_ratings, vsource_kw_ratings, rengen_kw_ratings, batt_kwh_ratings)
	rows = [
		'Diesel generation land use (acres)',
		'Diesel fuel storage land use (acres)',
		'Natural gas generation land use (acres)',
		'Natural gas fuel storage land use (acres)',
		'Solar generation land use (acres)',
		'Wind generation land use (acres)',
		'Battery storage land use (acres)',
		'Total with diesel (acres)',
		'Total with natural gas (acres)'
	]
	df = pd.DataFrame(index=rows)
	for mg in microgrids:
		diesel_gen_land_use = generation_land_use_acres.get('diesel_generation', {}).get(mg, 0)
		diesel_storage_land_use = fuel_land_use_acres.get('diesel_storage', {}).get(mg, 0)
		natural_gas_gen_land_use = generation_land_use_acres.get('natural_gas_generation', {}).get(mg, 0)
		natural_gas_storage_land_use = fuel_land_use_acres.get('natural_gas_storage', {}).get(mg, 0)
		solar_land_use = generation_land_use_acres.get('solar', {}).get(mg, 0)
		wind_land_use = generation_land_use_acres.get('wind', {}).get(mg, 0)
		battery_land_use = generation_land_use_acres.get('battery', {}).get(mg, 0)
		# Total land use for diesel
		total_with_diesel = sum([
			diesel_gen_land_use,
			diesel_storage_land_use,
			solar_land_use,
			wind_land_use,
			battery_land_use
		])
		# Total land use for natural gas
		total_with_natural_gas = sum([
			natural_gas_gen_land_use,
			natural_gas_storage_land_use,
			solar_land_use,
			wind_land_use,
			battery_land_use
		])
		df[mg] = [
			diesel_gen_land_use,
			diesel_storage_land_use,
			natural_gas_gen_land_use,
			natural_gas_storage_land_use,
			solar_land_use,
			wind_land_use,
			battery_land_use,
			total_with_diesel,
			total_with_natural_gas
		]
	# Add totals to final column
	df['Total'] = df.sum(axis=1)
	df = df.round(2)
	df = df.rename_axis('Microgrid ID').reset_index()
	table_html = '<h1>Land Use for Generation and Storage</h1>' + df.to_html(justify='left').replace('border="1"','border="0"')
	with open(out_html,'w') as outFile:
		outFile.write('<style>* {font-family:sans-serif}</style>' + '\n' + table_html)

def play(data, outage_start, outage_length, logger):
	'''
	Run a control simulation on circuit_plug_mgAll.dss
	'''
	assert isinstance(data, MappingProxyType)
	assert isinstance(outage_start, int)
	assert isinstance(outage_length, int)
	assert isinstance(logger, logging.Logger)
	path_to_dss = 'circuit_plus_mgAll.dss'
	faulted_lines = data['FAULTED_LINES']
	if not microgridup_validation.faulted_lines_in_graph(path_to_dss, faulted_lines):
		raise ValueError(f'One or more of the provided outage location(s) named "{faulted_lines}" are not in the provided circuit. Control simulation skipped.')
	microgrids = data['MICROGRIDS']
	print('CONTROLLING ON', microgrids)
	logger.warning(f'CONTROLLING ON {microgrids}')
	# - microgridup.py changes our directory to the one containing the currently running analysis. This is to help opendss run. If we're running this
	#   function by itself, we need to chdir into the work_dir argument.
	absolute_model_directory = f'{microgridup.PROJ_DIR}/{data["MODEL_DIR"]}'
	curr_dir = os.getcwd()
	if curr_dir != absolute_model_directory:
		os.chdir(absolute_model_directory)
	# Read in inputs.
	outage_end = outage_start + outage_length
	# Read in the circuit information.
	dssTree = dssConvert.dssToTree(path_to_dss)
	# - It seems like the only way to send fewer steps to newQstsPlot (outage_length + 48 steps) is to revise the timestamps fed to actions. Rather
	#   than giving actions the hours of year that represent outage start and outage end, feed actions 25 for the outage start and 25 + outage_length
	#   for the outage end.
	actions_outage_start = 25
	actions_outage_end = actions_outage_start + outage_length
	actions = {}
	# Add the fault, modeled as a 3 phase open, to the actions.
	open_line_actions = ''
	close_line_actions = ''
	for faulted_line in faulted_lines:
		open_line_actions += f'''
			open object=line.{faulted_line} term=1
			open object=line.{faulted_line} term=2
			open object=line.{faulted_line} term=3
		'''
		close_line_actions += f'''
			close object=line.{faulted_line} term=1
			close object=line.{faulted_line} term=2
			close object=line.{faulted_line} term=3
		'''
	actions[actions_outage_start] = open_line_actions
	actions[actions_outage_end] = close_line_actions
	actions[1] = ''
	# Initialize dict of vsource ratings 
	vsource_kw_ratings = defaultdict(dict)
	# Initialize dict of rengen mg labels and data.
	rengen_mgs = {}
	# Keep track of any renewable-only microgrids.
	rengen_fnames = []
	# Add per-microgrid objects, edits and actions.
	for mg_name, mg in microgrids.items():
		# - We get rid of every non-critical load to load shed
		for load_name in mg['loads']:
			if load_name not in data['CRITICAL_LOADS']:
				try:
					load = _getByName(dssTree, load_name)
					old_kw = load['kw']
					load['kw'] = '0'
					print(f'reduced {load_name} from {old_kw}kW to 0kW.')
					logger.warning(f'reduced {load_name} from {old_kw}kW to 0kW.')
				except:
					pass
		# - Have all microgrids switch out of the circuit during fault.
		switch_name = mg['switch']
		# HACK: we don't store whether the switch is a line or transformer in the microgrid dict, so make a best guess
		switch_type = _getByName(dssTree, switch_name)['object'].split('.')[0]
		actions[actions_outage_start] += f'''
			open object={switch_type}.{switch_name} term=1
			open object={switch_type}.{switch_name} term=2
			open object={switch_type}.{switch_name} term=3
		'''
		actions[actions_outage_end] += f'''
			close object={switch_type}.{switch_name} term=1
			close object={switch_type}.{switch_name} term=2
			close object={switch_type}.{switch_name} term=3
		'''
		# Get all microgrid fossil units, sorted by size
		gen_bus = mg['gen_bus']
		all_mg_fossil = [
			ob for ob in dssTree
			if ob.get('bus1','x.x').split('.')[0] == gen_bus
			and 'generator.fossil' in ob.get('object')
		]
		all_mg_fossil.sort(key=lambda x:float(x.get('kw')))
		# Insert a vsource for the largest fossil unit in each microgrid.
		if len(all_mg_fossil) > 0: # i.e. if we have a fossil generator
			manual_balance_approach = False
			# vsource variables.
			big_gen_ob = all_mg_fossil[-1]
			big_gen_index = dssTree.index(big_gen_ob)
			safe_busname = gen_bus.replace('.','_')
			vsource_ob_and_name = f'vsource.leadgen_{safe_busname}'
			line_name = f'line.line_for_leadgen_{safe_busname}'
			new_bus_name = f'bus_for_leadgen_{safe_busname}.1.2.3'
			#NOTE: vsources use line-to-line voltages, so 3 phase gens get sqrt(3) multiplier
			phase_count = big_gen_ob.get('phases')
			gen_base_kv_str = big_gen_ob.get('kv')
			try:
				gen_base_kv = float(gen_base_kv_str)
			except:
				print(f"HACK: no voltage detected for {vsource_ob_and_name} on {gen_bus} so falling back on default 4.16kv")
				logger.warning(f"HACK: no voltage detected for {vsource_ob_and_name} on {gen_bus} so falling back on default 4.16kv")
				gen_base_kv = 4.16
			if phase_count == '3':
				gen_base_kv = gen_base_kv * math.sqrt(3)
			# Before removing fossil unit, grab kW rating 
			vsource_kw_ratings[mg_name][f"leadgen_{safe_busname}"] = big_gen_ob.get("kw")
			# Remove fossil unit, add new gen and line
			del dssTree[big_gen_index]
			dssTree.insert(big_gen_index, {'!CMD':'new', 'object':vsource_ob_and_name, 'basekv':str(gen_base_kv), 'bus1':new_bus_name, 'phases':phase_count})
			dssTree.insert(big_gen_index, {'!CMD':'new', 'object':line_name, 'bus1':f'{gen_bus}.1.2.3', 'bus2':new_bus_name, 'switch':'yes'}) # Need phases on bus1 so dssConvert doesn't crash.
			# Disable the new lead gen by default.
			actions[1] += f'''
				open {line_name}
				calcv
			'''
			#Enable/disable the diesel vsources during the outage via actions.
			actions[actions_outage_start] += f'''
				close {line_name}
				calcv
			'''
			actions[actions_outage_end] += f'''
				open {line_name}
				calcv
			'''
		else:
			manual_balance_approach = True
		# Manually construct battery loadshapes for outage.
		# new_dss_tree, new_batt_loadshape, cumulative_existing_batt_shapes, all_rengen_shapes, total_surplus, all_loads_shapes_kw = do_manual_balance_approach(outage_start, outage_end, mg, dssTree, logger)
		compute_result = compute_manual_balance(outage_start, outage_end, mg, dssTree, logger)
		new_dss_tree = apply_manual_balance_to_tree(compute_result, dssTree, logger)
		new_batt_loadshape = compute_result['new_batt_loadshape_sliced']
		cumulative_existing_batt_shapes = compute_result['cumulative_existing_batt_shapes']
		all_rengen_shapes = compute_result['all_rengen_shapes']
		total_surplus = compute_result['total_surplus']
		all_loads_shapes_kw = compute_result['all_loads_shapes_kw']
		# Manual Balance Approach plotting call.
		if manual_balance_approach == True:
			fname = plot_manual_balance_approach(mg_name, 2019, outage_start, outage_end, outage_length, new_batt_loadshape, cumulative_existing_batt_shapes, all_rengen_shapes, total_surplus, all_loads_shapes_kw)
			rengen_fnames.append(fname)
			rengen_mgs[mg_name] = {"All loads loadshapes during outage (kw)":list(all_loads_shapes_kw[outage_start:outage_end]),
			"All rengen loadshapes during outage (kw)":list(all_rengen_shapes[outage_start:outage_end]),
			"Cumulative battery loadshape during outage (kw)":list(new_batt_loadshape),
			"Surplus rengen":total_surplus}
	# print("vsource_kw_ratings",vsource_kw_ratings)
	# print("rengen_mgs",rengen_mgs)
	# Additional calcv to make sure the simulation runs.
	actions[actions_outage_start] += f'calcv\n'
	actions[actions_outage_end] += f'calcv\n'
	# Write the adjusted opendss file with new kw, generators.
	dssConvert.treeToDss(new_dss_tree, 'circuit_control.dss')
	# Precompute element to mg mapping to be used for speedy lookup later.
	element_to_mg = precompute_mg_element_mapping('circuit_control.dss', microgrids, logger)
	# Run the simulation.  can hang, so wait at most 4 minutes for it to complete
	FPREFIX = 'timezcontrol'
	newQstsPlot(
		'circuit_control.dss',
		stepSizeInMinutes=60, 
		numberOfSteps=outage_length+48,
		keepAllFiles=False,
		actions=actions,
		filePrefix=FPREFIX
	)
	if os.path.exists(f'{FPREFIX}_gen.csv') and os.path.exists(f'{FPREFIX}_source.csv'):
		# Merge gen and source csvs for plotting fossil loading percentages
		inputs = [f'{FPREFIX}_gen.csv', f'{FPREFIX}_source.csv']
		# First determine the field names from the top line of each input file
		fieldnames = []
		for filename in inputs:
			with open(filename, "r", newline="") as f_in:
				reader = csv.reader(f_in)
				headers = next(reader)
				for h in headers:
					if h not in fieldnames:
						fieldnames.append(h)
		# Then copy the data
		with open(f"{FPREFIX}_source_and_gen.csv", "w", newline="") as f_out:
			writer = csv.DictWriter(f_out, fieldnames=fieldnames)
			writer.writeheader()
			for filename in inputs:
				with open(filename, "r", newline="") as f_in:
					reader = csv.DictReader(f_in)  # Uses the field names in this file
					for line in reader:
						writer.writerow(line)
	# Collect fossil_kw_ratings, batt_kwh_ratings, and rengen_kw_ratings.
	batt_kwh_ratings, fossil_kw_ratings, rengen_kw_ratings = defaultdict(dict), defaultdict(dict), defaultdict(dict)
	# Info we need from tree: every battery by name with corresponding kwhrated, every fossil gen by name with corresponding kw, every rengen by name with corresponding kw rated.
	for item in new_dss_tree:
		if "generator.fossil" in item.get("object","x.x"):
			mg = _get_mg_from_element_name(item.get('object').split('.')[1], element_to_mg)
			fossil_kw_ratings[mg][item.get("object").split(".")[1]] = item.get("kw")
		if "storage.battery" in item.get("object","x.x"):
			mg = _get_mg_from_element_name(item.get('object').split('.')[1], element_to_mg)
			batt_kwh_ratings[mg][item.get("object").split(".")[1]] = item.get("kwhrated")
		if "generator.solar" in item.get("object","x.x") or "generator.wind" in item.get("object","x.x"):
			mg = _get_mg_from_element_name(item.get('object').split('.')[1], element_to_mg)
			rengen_kw_ratings[mg][item.get("object").split(".")[1]] = item.get("kw")
	# Generate the output charts.
	if os.path.exists(f'{FPREFIX}_source_and_gen.csv'):
		diesel_dict, natural_gas_dict = make_chart_from_csv(f'{FPREFIX}_source_and_gen.csv', ['P1(kW)','P2(kW)','P3(kW)'], 2019, microgrids, "Generator Output", "Average Hourly kW", outage_start, outage_length, batt_kwh_ratings, fossil_kw_ratings, logger, vsource_kw_ratings=vsource_kw_ratings, rengen_kw_ratings=rengen_kw_ratings, rengen_mgs=rengen_mgs)
	if os.path.exists(f'{FPREFIX}_load.csv'):
		make_chart_from_csv(f'{FPREFIX}_load.csv', ['V1(PU)','V2(PU)','V3(PU)'], 2019, microgrids, "Load Voltage", "PU", outage_start, outage_length, batt_kwh_ratings, fossil_kw_ratings, logger, rengen_kw_ratings=rengen_kw_ratings, rengen_mgs=rengen_mgs)
	if os.path.exists(f'{absolute_model_directory}/{FPREFIX}_control.csv'):
		make_chart_from_csv(f'{FPREFIX}_control.csv', ['Tap(pu)'], 2019, microgrids, "Tap Position", "PU", outage_start, outage_length, batt_kwh_ratings, fossil_kw_ratings, logger)
	plot_inrush_data(path_to_dss, microgrids, f'{FPREFIX}_inrush_plot.html', outage_start, outage_end, outage_length, logger, vsource_kw_ratings=vsource_kw_ratings)
	# Make land use chart.
	make_land_use_chart(microgrids, f'{FPREFIX}_land_use_chart.html', fossil_kw_ratings, vsource_kw_ratings, rengen_kw_ratings, 
	batt_kwh_ratings, diesel_dict, natural_gas_dict)
	# Make sizing chart to accompany land use chart.
	make_sizing_chart(microgrids, f'{FPREFIX}_sizing_chart.html', fossil_kw_ratings, vsource_kw_ratings, rengen_kw_ratings, batt_kwh_ratings, diesel_dict, natural_gas_dict)
	# Write final output file.
	output_slug = '''	
		<head>
			<style type="text/css">
				* {font-family:sans-serif}
				iframe {width:100%; height:600px; border:0;}
			</style>
		</head>
		<body style="margin:0px">'''
	if os.path.isfile(f'{FPREFIX}_source_and_gen.csv.plot.html'):
		output_slug += f'''
			<iframe src="{FPREFIX}_source_and_gen.csv.plot.html"></iframe>'''
	if os.path.isfile(f'{FPREFIX}_load.csv.plot.html'):
		output_slug += f'''
			<iframe src="{FPREFIX}_load.csv.plot.html"></iframe>'''
	if os.path.isfile(f'{FPREFIX}_control.csv.plot.html'):
		output_slug += f'''
			<iframe src="{FPREFIX}_control.csv.plot.html"></iframe>'''
	if os.path.isfile(f'{FPREFIX}_source_and_gen.csv_battery_cycles.plot.html'):
		output_slug += f'''
			<iframe src="{FPREFIX}_source_and_gen.csv_battery_cycles.plot.html"></iframe>'''
	output_slug += '''
		</body>'''
	print(f'Control microgrids count {len(microgrids)} and renewable count {len(rengen_fnames)}')
	logger.warning(f'Control microgrids count {len(microgrids)} and renewable count {len(rengen_fnames)}')
	if len(microgrids) != len(rengen_fnames):
		if os.path.isfile(f'{FPREFIX}_source_and_gen.csv_fossil_loading.plot.html'):
			output_slug += f'''
			<iframe src="{FPREFIX}_source_and_gen.csv_fossil_loading.plot.html"></iframe>'''
		if os.path.isfile(f'{FPREFIX}_source_and_gen.csv_fuel_consumption.plot.html'):
			output_slug += f'''
			<iframe src="{FPREFIX}_source_and_gen.csv_fuel_consumption.plot.html"></iframe>'''
	for rengen_name in rengen_fnames:
		# insert renewable manual balance plots.
		output_slug += f'<iframe src="{rengen_name}"></iframe>'
	output_slug += f'<iframe src="{FPREFIX}_inrush_plot.html" style="height: 250px"></iframe>'
	output_slug += f'<iframe src="{FPREFIX}_land_use_chart.html" style="height: 300px"></iframe>'
	output_slug += f'<iframe src="{FPREFIX}_sizing_chart.html" style="height: 300px"></iframe>'
	with open('output_control.html','w') as outFile:
		outFile.write(output_slug)
	# Undo directory change.
	os.chdir(curr_dir)

def _tests():
	# Load lehigh4mg to use as test input
	test_model = 'lehigh4mgs'
	absolute_model_directory = f'{microgridup.PROJ_DIR}/{test_model}'
	# HACK: work in directory because we're very picky about the current dir.
	curr_dir = os.getcwd()
	if curr_dir != absolute_model_directory:
		os.chdir(absolute_model_directory)
	with open('allInputData.json') as file:
		immutable_data = microgridup.get_immutable_dict(json.load(file))
	mg_name = list(immutable_data['MICROGRIDS'].keys())[0]
	with open(f'reopt_{mg_name}/allInputData.json') as file:
		allInputData = json.load(file)
	outage_start = int(allInputData['outage_start_hour'])
	outage_length = int(allInputData['outageDuration'])
	logger = microgridup.setup_logging('logs.log')
	print(f'----------microgridup_control.py testing {test_model}----------')
	logger.warning(f'----------microgridup_control.py testing {test_model}----------')
	play(immutable_data, outage_start, outage_length, logger)
	os.chdir(curr_dir)
	return print('Ran all tests for microgridup_control.py.')

if __name__ == '__main__':
	_tests()