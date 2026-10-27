import os, json
from omf.solvers import opendss
import networkx as nx
from pprint import pprint as pp
from collections import defaultdict, deque
import microgridup
from microgridup_control import _parse_bus_field

# Auto gen some microgrid descriptions.
# See experiments with networkx here: https://colab.research.google.com/drive/1RZyD6pRIdRAT-V2sBB0nPKVIvZP_RGHw
# New colab with updated experiments: https://colab.research.google.com/drive/1j_u30UdnhaRovtG6Odhe_ivDKwBlc5qY?usp=sharing

# Test inputs
# CIRC_FILE = 'lehigh_base_3mg.dss'
# CRITICAL_LOADS = ['645_hangar','684_command_center', '611_runway','675a_hospital','634a_data_center', '634b_radar', '634c_atc_tower']
# ALGO = 'lukes' #'branch'

class SwitchNotFoundError(Exception):
	'''Used by get_edge_name() and passed to GUI to be caught by custom Flask errorhandler.'''
	pass

class CycleDetectedError(Exception):
	'''Used by topological_sort() and passed to GUI to be caught by custom Flask errorhandler.'''
	pass

class InsufficientBranchPointsError(Exception):
	'''Used by nx_group_branch() and passed to GUI to be caught by custom Flask errorhandler.'''

def detect_cycle_dfs(G, node, visited, stack, cycle_nodes):
	'''Used by topological_sort function to identify cycles using dfs.'''
	if node in stack: # Cycle detected.
		cycle_start_index = stack.index(node)
		cycle_nodes.extend(stack[cycle_start_index:])
		return True
	if node in visited:
		return False # In the event that a node has multiple parents.
	visited.add(node)
	stack.append(node)
	for child in G[node]:
		if detect_cycle_dfs(G, child, visited, stack, cycle_nodes):
			return True
	stack.pop()
	return False

def topological_sort(G):
	'''Standardizes topological sort across networkx versions.'''
	in_degree = {node: 0 for node in G}
	for node in G:
		for child in G[node]:
			in_degree[child] += 1
	queue = deque([node for node in in_degree if in_degree[node] == 0])
	result = []
	visited = set()
	stack = []
	cycle_nodes = []
	for node in G:
		if node not in visited:
			if detect_cycle_dfs(G, node, visited, stack, cycle_nodes):
				raise CycleDetectedError(f'Graph contains a cycle: {cycle_nodes}')
	while queue:
		node = queue.popleft()
		result.append(node)
		for child in G[node]:
			in_degree[child] -= 1
			if in_degree[child] == 0:
				queue.append(child)
	if len(result) != len(G):
		raise CycleDetectedError('Graph contains a cycle, but it could not be fully identified')
	return result

def remove_loops(G):
	'''Helper function to remove cycles and loops from a graph using hacky topological sort because nx_topological_sort breaks on cycles. Used by lukes, branch, bottom up, and critical load algorithms.'''
	# Delete edges to all parents except the first in topological order.
	H = G.copy()
	order = []
	for k,v in nx.dfs_successors(H).items():
		if k not in order:
			order.append(k)
		for v_ in v:
			if v_ not in order:
				order.append(v_)
	for node in order: 
		parents = list(H.predecessors(node))
		if len(parents) > 1:
			ordered_parents = [x for x in order if x in parents]
			for idx in range(1,len(ordered_parents)):
				H.remove_edge(ordered_parents[idx],node)
		# Origin shouldn't have any parents. TO DO: Figure out why this sometimes breaks circuits with cycles to the source.
		# elif node == order[0] and len(parents) == 1:
			# G.remove_edge(parents[0],node)
	return H

def get_object_type_from_omd(node_name, omd):
	'''Get object type from omd given name. Helper to branch and bottom up algorithms.'''
	for key in omd:
		if omd[key].get('name') == node_name:
			return omd[key].get('object')
	return None 

def get_bus_nodes(G, omd):
	'''Get all bus nodes excluding the root node (precautionary, since root node tends to be a substation). Helper to branch and bottom up algorithms.'''
	tree_root = list(topological_sort(G))[0]
	subgraph = set(G.nodes()) - {tree_root}
	bus_nodes = set(node for node in subgraph if get_object_type_from_omd(node, omd) == 'bus')
	return bus_nodes

def only_child(G, mgs):
	'''Repeatedly adds parents to mgs defaultdict if parents do not have other children. Helper to bottom up and critical load algorithms.'''
	for key in list(mgs.keys()):
		parent = list(G.predecessors(key))
		if not parent:
			continue
		if len(parent) > 1:
			continue 
		grandparent = list(G.predecessors(parent[-1]))
		if not grandparent:
			continue # Do not want to include root node in microgrid (no viable switch).
		siblings = list(G.successors(parent[0]))
		while parent and len(siblings) == 1:
			mgs[parent[0]].append(siblings[0])
			mgs[parent[0]].extend(mgs[siblings[0]])
			del mgs[siblings[0]]
			parent = list(G.predecessors(parent[0]))
			grandparent = list(G.predecessors(parent[-1]))
			if not parent or len(parent) > 1 or not grandparent:
				break
			siblings = list(G.successors(parent[0]))
	return mgs

def loop_avoider(G, mgs):
	'''Check to see if a node has multiple parents. If so, add nodes to mgs defaultdict until LCA node of original parents is key in mgs. Helper to bottom up and critical load algorithms.'''
	bustworthy_nodes = set()
	
	def helper(node, isKey):
		bustworthy_nodes.add(node)
		# We have a loop on our hands. 
		lca = nx.lowest_common_ancestor(G, parents[0], parents[1]) # Assumption: only need to find LCA of two parents. TO DO: verify this assumption.
		lca_succs = list(nx.nodes(nx.dfs_tree(G, lca)))
		mgs[parents[0]].append(node)
		mgs[parents[0]].extend(mgs[node])
		mgs[parents[1]].extend([])
		if isKey:
			del mgs[node]
		new_keys = [x for x in topological_sort(G) if x in list(mgs.keys())] 
		for k in new_keys:
			if k in lca_succs:
				vals = mgs[k]
				pointer = k
				while pointer != lca: 
					vals.append(pointer)
					new_parents = list(G.predecessors(pointer))
					if len(new_parents) > 1:
						break 
					pointer = new_parents[0]
				mgs[lca].extend(vals)
				del mgs[k]

	# If a node has multiple parents, must find lowest common ancestor and make this node new key in mgs.
	keys = [x for x in topological_sort(G) if x in list(mgs.keys())]
	for key in keys:
		parents = list(G.predecessors(key))
		if len(parents) > 1:
			helper(key, True)
	# Did we miss any loops in the new values?
	for value in list(mgs.values()):
		for node in value:
			parents = list(G.predecessors(node))
			if len(parents) > 1 and node not in bustworthy_nodes:
				helper(node, False)
	return mgs

def nx_group_lukes(G, size, node_weight=None, edge_weight=None):
	'''Partition the graph using Lukes algorithm into pieces of [size] nodes.'''
	if not nx.is_tree(G):
		G = remove_loops(G)
	tree_root = list(topological_sort(G))[0]
	G_topo_order = nx.DiGraph(nx.algorithms.traversal.breadth_first_search.bfs_edges(G, tree_root))
	return nx.algorithms.community.lukes.lukes_partitioning(G_topo_order, size, node_weight=node_weight, edge_weight=edge_weight)

def nx_get_branches(G, min_out_degree=2):
	'''All branchy boys. Helper to branch algorithm.'''
	return [n for n in G.nodes() if G.out_degree(n) > min_out_degree]

def nx_group_branch(G, i_branch=0, omd={}):
	'''Create graph subgroups at branch point i_branch (topological order).'''
	if not nx.is_tree(G):
		G = remove_loops(G)
	tree_root = list(topological_sort(G))[0]
	edges_in_order = nx.DiGraph(nx.algorithms.traversal.breadth_first_search.bfs_edges(G, tree_root))
	bbl = nx_get_branches(edges_in_order)
	bbl_indices_to_delete = set() # Add a check to ensure branch algo does not consider item level for branching when user created circuit with wizard.
	for idx, bbl_item in enumerate(bbl):
		if bbl_item.endswith('_end'):
			feeder_name = bbl_item[0:-4]
			ob_type = get_object_type_from_omd(feeder_name, omd)
			if ob_type == 'line':
				bbl_indices_to_delete.add(idx)
	new_bbl = [item for idx, item in enumerate(bbl) if idx not in bbl_indices_to_delete]
	if len(new_bbl) == 0:
		first_branch = list(topological_sort(G))[0] # Consider all but the substation as a microgrid if there are no branching points.
	else:
		first_branch = new_bbl[i_branch]
	succs = list(G.successors(first_branch))
	parts = [list(nx.algorithms.traversal.depth_first_search.dfs_tree(G, x).nodes()) for x in succs]
	parts_no_regcontrol_mgs = [part for part in parts if not (len(part) == 1 and get_object_type_from_omd(part[0], omd) == 'regcontrol')] # regcontrol objects should not be their own microgrids alone.
	return parts_no_regcontrol_mgs

def branch_recursively(G, branch_points, tree_root, subgraph, grouped_nodes):
	'''Helper to new_nx_group_branch().'''
	if not subgraph:
		return []
	if branch_points:
		furthest_branch_point = branch_points[-1]
		branch_points.pop()
	else:
		return []
	descendants = list(nx.descendants(G, furthest_branch_point))
	current_group = [furthest_branch_point] + [node for node in descendants if node not in grouped_nodes]
	grouped_nodes.update(current_group) # Mark as grouped.
	remaining_nodes = [node for node in subgraph if node not in grouped_nodes]
	return [current_group] + branch_recursively(G, branch_points, tree_root, remaining_nodes, grouped_nodes)

def new_nx_group_branch(G, mg_quantity, omd={}):
	'''Branch recursively starting at lowest branch points on tree as specified by mg_quanity (that are buses) moving upwards.'''
	if not nx.is_tree(G):
		G = remove_loops(G)
	tree_root = list(topological_sort(G))[0]
	edges_in_order = nx.DiGraph(nx.algorithms.traversal.breadth_first_search.bfs_edges(G, tree_root))
	branch_points = nx_get_branches(edges_in_order)
	if tree_root in branch_points:
		branch_points.remove(tree_root)
	if len(branch_points) > mg_quantity:
		branch_points = branch_points[:mg_quantity]
	elif mg_quantity > len(branch_points):
		raise InsufficientBranchPointsError(f'Requested microgrid quantity is greater than number of branch points in circuit ({len(branch_points)} branch points). Please reduce the number of microgrids requested.')
	grouped_nodes = set()
	bus_nodes = get_bus_nodes(G, omd) # Only branch on buses. Doesn't affect outcome unless there is a branch point that isn't a bus (unlikely). Alternative is passing a subgraph of all nodes except root instead of bus_nodes to branch_recursively().
	parts = branch_recursively(G, branch_points, tree_root, bus_nodes, grouped_nodes)
	return parts

def relatable_siblings(G, mgs, bus_nodes):
	'''Merges neighboring microgrids in mgs defaultdict if all children of a node are buses and keys in mgs defaultdict. Helper to bottom up algorithm.'''
	items = [x for x in topological_sort(G) if x in list(mgs.keys())]
	for key in items:
		if key not in mgs:
			continue
		parent = list(G.predecessors(key))
		if len(parent) > 1:
			continue
		siblings = list(G.successors(parent[0]))
		bus_siblings = [node for node in siblings if node in bus_nodes]
		if all(sibling in mgs for sibling in bus_siblings):
			# Merge microgrids 
			for sibling in siblings:
				mgs[parent[0]].append(sibling)
				mgs[parent[0]].extend(mgs[sibling]) 
				del mgs[sibling]
	return mgs

def get_initial_mgs(G, bus_nodes):
	'''Find buses with nodes with parents but no children. Helper to bottom up algorithm.'''
	mgs = defaultdict(list)
	for node in bus_nodes:
		# Make the node an mg if it has no children or the children it has have no children.
		children = list(G.successors(node))
		if not children:
			mgs[node] = []
		else:
			for child in children:
				if list(G.successors(child)):
					break
			else:
				mgs[node] = children
	return mgs

def nx_bottom_up_branch(G, num_mgs=3, large_or_small='large', omd={}, cannot_be_mg=[]):
	'''Form all microgrid combinations starting with leaves and working up to source maintaining single points of connection for each.'''
	try:
		list(topological_sort(G))
	except CycleDetectedError:
		G = remove_loops(G)
	bus_nodes = get_bus_nodes(G, omd)
	mgs = get_initial_mgs(G, bus_nodes)
	mgs = only_child(G, mgs)
	parts = defaultdict(list)
	for key in sorted(mgs):
		parts[0].append([key])
		parts[0][-1].extend(mgs[key])
	counter = 1
	while len(mgs) > 1:
		mgs = loop_avoider(G, mgs)
		mgs = relatable_siblings(G, mgs, bus_nodes)
		mgs = only_child(G, mgs)
		if len(mgs) < num_mgs:
			break
		for key in sorted(mgs): 
			parts[counter].append([key])
			parts[counter][-1].extend(mgs[key])
		counter += 1
		if len(mgs) == num_mgs and large_or_small == 'small':
			break
	return parts[len(parts)-1]

def merge_mgs(G, mgs):
	'''
	Helper to critical load algorithm.
	Check to see if parent is a key in a microgrid. If so, merge microgrids.
	Check to see if parent is a value in a microgrid. If so, merge microgrids.
	Check to see if parent is parent of other most ancestral node in a microgrid. If so, annex parent and merge microgrids.
	Check to see if parent has no successors in any other mg. If so, annex parent.
	'''
	items = [x for x in topological_sort(G) if x in list(mgs.keys())]
	for k in items:
		if k not in mgs.keys():
			continue
		parent = list(G.predecessors(k))
		if not parent:
			continue
		parent = parent[0]
		inValues = [key for key, value in mgs.items() if parent in value]
		otherParent = [key for key, value in mgs.items() if list(G.predecessors(key)) == [parent] and key != k]
		otherKeys = [key for key, value in mgs.items() if key in list(nx.nodes(nx.dfs_tree(G, parent))) and key != k]
		otherValues = [key for key, value in mgs.items() if (set(value) & set(list(nx.nodes(nx.dfs_tree(G, parent))))) and key != k]
		otherMgs = otherKeys + otherValues
		if parent in mgs.keys() or inValues:
			# If the parent is already in a microgrid, merge with existing microgrid.
			if inValues: 
				mgs[inValues[0]].append(k)
				mgs[inValues[0]].extend(mgs[k])
				del mgs[k]
			else:
				mgs[parent].append(k)
				mgs[parent].extend(mgs[k]) 
				del mgs[k]
		elif otherParent:
			# Else if parent is also the parent of a different microgrid’s most ancestral node, annex parent and merge microgrids.
			mgs[parent].append(k)
			mgs[parent].append(otherParent[0])
			mgs[parent].extend(mgs[k])
			mgs[parent].extend(mgs[otherParent[0]])
			del mgs[k]
			del mgs[otherParent[0]]
		elif not otherMgs:
			# Else if parent has no successors in any other microgrid, annex parent.
			mgs[parent].append(k)
			mgs[parent].extend(mgs[k])
			del mgs[k]
	return mgs

def nx_critical_load_branch(G, criticalLoads, num_mgs=3, large_or_small='large'):
	'''Form all microgrid combinations prioritizing only critical loads and single points of connection.'''
	# Find all critical loads. They get a microgrid each. Output.
	critical_nodes = [[x] for x in G.nodes() if x in criticalLoads]
	try:
		top_down = list(topological_sort(G))
	except CycleDetectedError:
		G = remove_loops(G)
		top_down = list(topological_sort(G))
	mgs = defaultdict(list)
	for node in critical_nodes:
		mgs[node[-1]] = []
	# Include as many parents and parents of parents etc. in microgrid such that these parents have no other children.
	mgs = only_child(G, mgs)
	parts = defaultdict(list)
	for key in mgs:
		parts[0].append([key])
		parts[0][-1].extend(mgs[key])
	counter = 1
	while len(mgs) > 1:
		mgs = loop_avoider(G, mgs)
		mgs = merge_mgs(G, mgs)
		mgs = only_child(G, mgs)
		if len(mgs) < num_mgs:
			break
		for key in mgs:
			parts[counter].append([key])
			parts[counter][-1].extend(mgs[key])
		counter += 1
		if len(mgs) == num_mgs and large_or_small == 'small':
			break
	return parts[len(parts)-1]

def check_if_loads_in_tree(G, pairs):
	'''Identify between pairings in current tree (of forest) and pairs outside of current tree. Helper to load grouping algorithm.'''
	nodes_list = list(G.nodes())
	loads_in_tree = []
	nodes_not_in_tree = []
	for node in pairs:
		if node in nodes_list:
			loads_in_tree.append(node)
		else:
			nodes_not_in_tree.append(node)
	return loads_in_tree, nodes_not_in_tree

def load_grouping(G, pairings):
	'''Accepts loads paired to each microgrid and uses networkx to infer other existing objects in that subtree of the circuit.
	Graph G cannot contain loops.'''
	# Create reference dict of lcas for each pair of two nodes.
	tree_root = list(topological_sort(G))[0]
	lcas = nx.tree_all_pairs_lowest_common_ancestor(G, root=tree_root)
	lcas = dict(lcas)
	# Iterate through each MG's loads, comparing LCAs each time to find new overall LCA.
	mgs = {}
	pairings.pop('None', None)
	for mg in pairings:
		pairs = pairings[mg]
		loads_in_tree, nodes_not_in_tree = check_if_loads_in_tree(G, pairs)
		if not loads_in_tree:
			print(f'None of the pairs are in the current tree. Skipping.')
			continue
		if nodes_not_in_tree:
			print(f'Error: the following nodes are not in current tree: {nodes_not_in_tree}.')
		if len(pairs) > 1:
			cur_lca = lcas.get((pairs[0],pairs[1]),lcas.get((pairs[1],pairs[0])))
		elif len(pairs) == 1: # If there is only one load in a mg, use its parent as LCA. TO DO: Verify this assumption.
			cur_lca = list(G.predecessors(pairs[0]))[0]
		else:
			print('Error: Amount of loads in each microgrid must be greater than 0.')
		for idx in range(2,len(pairs)):
			cur_lca = lcas.get((cur_lca,pairs[idx]),lcas.get((pairs[idx],cur_lca)))
		mgs[mg] = list(nx.nodes(nx.dfs_tree(G, cur_lca)))
	# Only return the contents of each MG but do so in order specified by the user.
	parts = []
	counter = 0
	while mgs:
		key = f'mg{counter}'
		if mgs.get(key):
			parts.append(mgs[key])
			del mgs[key]
		counter += 1
	return parts

def manual(pairings, gen_obs_existing):
	'''Partitioning without networkx. Accepts dict of loads paired by mg and dict of gen_obs_existing paired by mg from user input.'''
	pairings.pop('None', None)
	parts = []
	sorted_mg_ids = sorted(pairings.keys(), key=lambda mg: int(mg[2:]))
	for mg_id in sorted_mg_ids:
		loads = pairings[mg_id]
		mg_gen_obs_existing = gen_obs_existing[mg_id].split(',')
		parts.append(loads + mg_gen_obs_existing)
	return parts

def get_all_trees(F):
	'''A function to get all trees in a forest. Used when forming mg_groups.'''
	list_of_trees = list(nx.weakly_connected_components(F))
	return [F.subgraph(tree) for tree in list_of_trees]

def form_mg_groups(G, CRITICAL_LOADS, algo, algo_params={}):
	'''
	Generate a group of mgs from networkx graph and crit_loads
	algo must be one of ["lukes", "branch", "bottomUp", "criticalLoads", "loadGrouping", "manual"]
	lukes algo params is 'size':int giving size of each mg.
	branch algo params is 'i_branch': giving which branch in the tree to split on.
	'''
	MG_GROUPS = []
	if algo == 'manual': # Manual partitioning bypasses networkx completely.
		MG_GROUPS = manual(algo_params['pairings'], algo_params['gen_obs_existing'])
	else:
		all_trees = get_all_trees(G)
		all_trees_pruned = [tree for tree in all_trees if len(tree.nodes()) > 1]
		num_trees_pruned = len(all_trees_pruned)
		for tree in all_trees_pruned:
			if algo == 'lukes':
				default_size = int(len(tree.nodes())/3)
				MG_GROUPS.extend(nx_group_lukes(tree, algo_params.get('size',default_size)))
			elif algo == 'branch':
				MG_GROUPS.extend(nx_group_branch(tree, i_branch=algo_params.get('i_branch',0), omd=algo_params.get('omd',{})))
			elif algo == 'bottomUp':
				MG_GROUPS.extend(nx_bottom_up_branch(tree, num_mgs=algo_params.get('num_mgs',3)/num_trees_pruned, large_or_small='large', omd=algo_params.get('omd',{}), cannot_be_mg=algo_params.get('cannot_be_mg',[])))
			elif algo == 'criticalLoads':
				MG_GROUPS.extend(nx_critical_load_branch(tree, CRITICAL_LOADS, num_mgs=algo_params.get('num_mgs',3)/num_trees_pruned, large_or_small='large'))
			elif algo == 'loadGrouping':
				MG_GROUPS.extend(load_grouping(tree, algo_params['pairings']))
			else:
				print('Invalid algorithm. algo must be "branch", "lukes", "bottomUp", "criticalLoads", "loadGrouping", or "manual". No mgs generated.')
				return {}
	return MG_GROUPS

def nx_out_edges(G, sub_nodes):
	'''Find edges connect sub_nodes to rest of graph. Helper to form_microgrids().'''
	mg1 = G.subgraph(sub_nodes)
	out_edges = []
	for n in mg1.nodes():
		out_preds = [(x,n) for x in G.predecessors(n) if x not in mg1]
		# out_succ = [(n,x) for x in G.successors(n) if x not in mg1] # Thomas 8/9/24: our current partitioning algos operate on the assumption that a single point of connection to the rest of the circuit exists 'before' the mg when sorted topologically. Out edges may exist 'after' the mg, but should only be considered in the control module.
		# out_edges.extend(out_succ)
		out_edges.extend(out_preds)
	out_edges.sort()
	return out_edges

def get_edge_name(fr, to, omd_list):
	'''Get an edge name using (fr,to) in the omd_list. Helper to form_microgrids().'''
	edges = [ob.get('name') for ob in omd_list if ob.get('from') == fr and ob.get('to') == to]
	if len(edges) > 0:
		return edges[0]
	# If no explicit edge between fr->to (common when loads/gens live on bus), try to find a line (or transformer) that feeds the bus 'fr' (i.e., line with to == fr).
	feeders = [ob.get('name') for ob in omd_list if ob.get('object','').startswith('line') and ob.get('to') == fr]
	if len(feeders) > 0:
		# If more than one feeder found, prefer the first.
		return feeders[0]
	# Try transformers feeding the bus.
	transformers = []
	for ob in omd_list:
		if 'transformer' in ob.get('object',''):
			buses_field = ob.get('buses') or ob.get('bus') or ob.get('bus2')
			if buses_field:
				# Handle different formats: '[bus1,bus2]' or 'bus1,bus2' or list.
				buses_str = buses_field if isinstance(buses_field, str) else ','.join(buses_field)
				parts = _parse_bus_field(buses_field)
				if len(parts) >= 2:
					buses_str = parts[1]
				elif len(parts) == 1:
					buses_str = parts[0]
				# Naive contains check; more parsing could be added if needed.
				if fr in buses_str:
					transformers.append(ob.get('name') or ob.get('object').split('.')[1])
	if len(transformers) > 0:
		return transformers[0]
	# If still not found, raise error.
	raise SwitchNotFoundError(f'Selected partitioning method produced invalid results. No valid switch found between {fr} and {to}. Please change partitioning parameter(s).')

def form_microgrids(G, MG_GROUPS, omd, switch_dict=None, gen_bus_dict=None, mg_name_dict=None):
	'''
	Generate microgrid data structure from a networkx graph, group of mgs, and omd. 
	Optional parameters switch and gen_bus may be supplied when loadGrouping or manual partitioning is used.
	'''
	omd_list = list(omd.values())
	all_mgs = [
		(M_ID, MG_GROUP, MG_GROUP[0], nx_out_edges(G, MG_GROUP))
		for (M_ID, MG_GROUP) in enumerate([list(x) for x in MG_GROUPS])
	]
	MICROGRIDS = {}
	for idx in range(len(all_mgs)):
		M_ID, MG_GROUP, TREE_ROOT, BORDERS = all_mgs[idx]
		mg_name = mg_name_dict[f'mg{M_ID}'] if mg_name_dict and mg_name_dict[f'mg{M_ID}'] else f'mg{M_ID}'
		# Determine the switch(s) for borders, but be tolerant if get_edge_name raises
		if switch_dict and switch_dict.get(f'mg{M_ID}') not in (None, [''], ''):
			this_switch = switch_dict[f'mg{M_ID}']
		else:
			this_switch = []
			for swedge in BORDERS:
				try:
					this_switch.append(get_edge_name(swedge[0], swedge[1], omd_list))
				except SwitchNotFoundError as e:
					print(f'Error: {e}')
		# Normalize switch if it's a list.
		if this_switch and isinstance(this_switch, list):
			this_switch = this_switch[-1] if this_switch[-1] else this_switch[0]
		# Determine gen_bus: allow explicit gen_bus_dict override first
		if gen_bus_dict and gen_bus_dict.get(f'mg{M_ID}') not in (None, ''):
			this_gen_bus = gen_bus_dict[f'mg{M_ID}']
		else:
			# Infer gen bus for all objects in MG_GROUP by looking at their 'parent' fields in omd_list. Gen bus = parent of first object in MG_GROUP to have a parent. Note that buses do not have parents.
			parent_buses = set()
			for name in MG_GROUP:
				# Find matching object in omd_list.
				for ob in omd_list:
					if ob.get('name') == name:
						# loads/generators/storage often have 'parent' or 'bus1'
						parent = ob.get('parent') or ob.get('bus') or ob.get('bus1') or ob.get('parent_bus')
						if parent:
							# Strip phase suffix if present (e.g., 'reg0_end.1' -> 'reg0_end')
							parent_buses.add(str(parent).split('.')[0])
						break
			# If exactly one parent bus and it's not empty, prefer that as gen_bus.
			if len(parent_buses) == 1:
				this_gen_bus = list(parent_buses)[0]
			else:
				# Fallback to TREE_ROOT (sometimes problematic).
				this_gen_bus = TREE_ROOT
		# MICROGRIDS[f'mg{M_ID}'] = {
		MICROGRIDS[mg_name] = {
			'loads': [ob.get('name') for ob in omd_list if ob.get('name') in MG_GROUP and ob.get('object') == 'load'],
			'switch': this_switch, 
			'gen_bus': this_gen_bus,
			'gen_obs_existing': [ob.get('name') for ob in omd_list if ob.get('name') in MG_GROUP and ob.get('object') in ('generator','storage')]
		}
	# Validation checks.
	for mg_name, mg in MICROGRIDS.items():
		if not mg.get('switch'):
			msg = f'Partitioning method produced microgrid "{mg_name}" with no switch. Please choose a different partitioning method or change other partitioning parameters.'
			print(msg)
			raise SwitchNotFoundError(msg)
		if not mg.get('loads'):
			msg = f'Partitioning method produced microgrid "{mg_name}" with no loads. Please choose a different partitioning method or ensure each microgrid has at least one load.'
			print(msg)
			raise ValueError(msg)
	return MICROGRIDS

def _tests():
	with open(f'{microgridup.MGU_DIR}/testfiles/test_params.json') as file:
		test_params = json.load(file)
	MICROGRIDS = test_params['MICROGRIDS']
	algo_params = test_params['algo_params']
	crit_loads = test_params['crit_loads']
	lehigh_dss_path = f'{microgridup.MGU_DIR}/testfiles/lehigh_base_phased.dss'
	wizard_dss_path = f'{microgridup.MGU_DIR}/testfiles/wizard_base_3mg.dss'
	# Testing microgridup_gen_mgs.mg_group().
	for _dir in MICROGRIDS:
		partitioning_method = MICROGRIDS[_dir][1]
		# HACK: loadGrouping accepts pairings but not gen_obs_existing. manual accepts both. Other algos accept neither. 
		if partitioning_method == 'loadGrouping': 
			params = algo_params.copy()
			del params['gen_obs_existing']
		elif partitioning_method == 'manual':
			params = algo_params.copy()
			if 'wizard' in _dir:
				del params['gen_bus']
				del params['switch']
		else:
			params = {}
		try:
			_dir.index('wizard')
			G = opendss.dssConvert.dss_to_networkx(wizard_dss_path)
			omd = opendss.dssConvert.dssToOmd(wizard_dss_path, '', RADIUS=0.0004, write_out=False)
			params['omd'] = omd
			MG_GROUPS_TEST = form_mg_groups(G, crit_loads, partitioning_method, params)
			MICROGRIDS_TEST = form_microgrids(G, MG_GROUPS_TEST, omd) if partitioning_method != 'manual' else form_microgrids(G, MG_GROUPS_TEST, omd, params.get('switch'), params.get('gen_bus')) # No valid gen_bus in MG_GROUPS_TEST. When using manual partitioning, the user should specify gen_bus and switch.
		except ValueError:
			G = opendss.dssConvert.dss_to_networkx(lehigh_dss_path)
			omd = opendss.dssConvert.dssToOmd(lehigh_dss_path, '', RADIUS=0.0004, write_out=False)
			params['omd'] = omd
			MG_GROUPS_TEST = form_mg_groups(G, crit_loads, partitioning_method, params)
			MICROGRIDS_TEST = form_microgrids(G, MG_GROUPS_TEST, omd, params.get('switch'), params.get('gen_bus'))
		# - Austin (8/1/2024): the "parameter_overrides" key is now a part of the schema for microgrid dicts. The "parameter_overrides" key is
		#   only used to store information about REopt parameters that are overridden on a per-microgrid basis by the user through the front-end
		#   GUI or through Python. I add the "parameter_overrides" key here so that these tests pass, but the key could be added to the microgrid
		#   generation code in this module if desired, in which case these lines of code could be removed
		for mg in MICROGRIDS_TEST.values():
			mg['parameter_overrides'] = {
				'reopt_inputs': {}
			}
		# Lukes algorithm outputs different configuration each time. Not repeatable. Also errors out later in the MgUP run.
		assert MICROGRIDS_TEST == MICROGRIDS[_dir][0], f'MGU_MICROGRIDS_{_dir} did not match expected output.\nExpected output: {MICROGRIDS[_dir][0]}.\nReceived output: {MICROGRIDS_TEST}.'
	return print('Ran all tests for microgridup_gen_mgs.py.')

if __name__ == '__main__':
	_tests()