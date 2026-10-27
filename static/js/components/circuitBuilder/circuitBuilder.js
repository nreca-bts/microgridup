export { CircuitElement, CircuitModel, CircuitController, CircuitTableView, CircuitUserControlsView, CsvLoadParser, OutageLocationInputView };
import { Modal, getTrashCanSvg } from '../modal.js';

class CircuitElement {
    
    // - Namespaces used to match OpenDSS, but David wants a global uniqueness constraint, so now there's only one namespace
    //static namespaces = ['substation', 'feeder', 'load', 'generator', 'storage'];
    static namespaces = ['element'];
    static types = ['substation', 'feeder', 'load', 'solar', 'wind', 'fossil', 'battery'];
    #meta;
    #props;

    /**
     * - The "meta" namespace is for properties that aren't inherent to the CircuitElement itself
     * - The "props" namespace is for properties that are part of the CircuitElement
     * - TODO: add coordinates
     */
    constructor(props, meta=null) {
        if (typeof props !== 'object') {
            throw Error('The "props" argument must be typeof "object".');
        }
        if (!props.hasOwnProperty('name')) {
            throw Error('The "props" argument is missing required property "name".');
        }
        if (!props.hasOwnProperty('namespace')) {
            throw Error('The "props" argument is missing required property "namespace".');
        }
        if (!props.hasOwnProperty('type')) {
            throw Error('The "props" argument is missing required property "type".');
        }
        for (const [key, val] of Object.entries(props)) {
            CircuitElement.validateProperty(key, val, 'props');
        }
        this.#props = props;
        if (meta !== null) {
            if (typeof meta !== 'object') {
                throw TypeError('The "meta" argument must be null or typeof "object".');
            }
            for (const [key, val] of Object.entries(meta)) {
                CircuitElement.validateProperty(key, val, 'meta');
            }
            this.#meta = meta;
        } else {
            this.#meta = {};
        }
    }

    getProperty(property, namespace='props') {
        if (typeof property !== 'string') {
            throw TypeError('The "property" argument must be typeof "string".');
        }
        if (!['meta', 'props'].includes(namespace)) {
            throw Error('The "namespace" argument must be one of "meta" or "props".');
        }
        if (namespace === 'meta') {
            if (this.#meta.hasOwnProperty(property)) {
                return this.#meta[property];
            } else {
                throw Error(`The property "this.#meta.${property}" does not exist.`);
            }
        } else if (namespace === 'props') {
            if (this.#props.hasOwnProperty(property)) {
                return this.#props[property];
            } else {
                throw Error(`The property "this.#props.${property}" does not exist.`);
            }
        }
        throw Error();
    }

    getProperties(namespace='props') {
        if (!['meta', 'props'].includes(namespace)) {
            throw Error('The "namespace" argument must be one of "meta" or "props".');
        }
        if (namespace === 'meta') { 
            return this.#meta;
        } else if (namespace === 'props') {
            return this.#props;
        }
        throw Error();
    }

    hasProperty(property, namespace='props') {
        if (typeof property !== 'string') {
            throw TypeError('The "property" argument must be typeof "string".');
        }
        if (!['meta', 'props'].includes(namespace)) {
            throw Error('The "namespace" argument must be one of "meta" or "props".');
        }
        if (namespace === 'meta') {
            return this.#meta.hasOwnProperty(property);
        } else if (namespace === 'props') {
            return this.#props.hasOwnProperty(property);
        }
        throw Error();
    }

    setProperty(property, propertyVal, namespace='props') {
        if (typeof property !== 'string') {
            throw TypeError('The "property" argument must be typeof "string".');
        }
        if (!['meta', 'props'].includes(namespace)) {
            throw Error('The "namespace" argument must be one of "meta" or "props".');
        }
        if (propertyVal === undefined) {
            if (namespace === 'meta') {
                delete this.#meta[property];
            } else {
                delete this.#props[property];
            }
        } else {
            CircuitElement.validateProperty(property, propertyVal, namespace);
            if (namespace === 'meta') {
                this.#meta[property] = propertyVal;
            } else {
                this.#props[property] = propertyVal;
            }
        }
    }

    get fullName() {
        return `${this.getProperty('namespace')}.${this.getProperty('name')}`;
    }

    set fullName(val) {
        throw Error('The "fullName" property is read-only because it is a derived property composed of the "namespace" and "name" of an element. It cannot be directly set.');
    }

    static validateProperty(property, propertyVal, namespace) {
        //const onlyLetters = /^[A-Za-z\-_]+$/;
        const onlyNumbers = /^(?:0(?:\.\d+)?|[1-9]\d*(?:\.\d+)?)$/;
        const lettersOrNumbers = /^[\w\-]+$/;
        const onlyIntegers = /^\d+$/;
        if (namespace === 'props') {
            if (['name', 'namespace', 'type'].includes(property)) {
                if (typeof propertyVal !== 'string') {
                    throw TypeError(`The "${property}" property must be typeof "string".`);
                }
                if (!lettersOrNumbers.test(propertyVal)) {
                    // - CSVs heading load names may contain uppercase letters. I raise an error if two headings are the same in any case
                    throw Error(`Value can only include (1) alphanumeric characters, (2) "-", and (3) "_". Please change the value.`);
                }
                if (property === 'namespace') {
                    if (!CircuitElement.namespaces.includes(propertyVal)) {
                        throw Error('The "props.namespace" value must be one of the values in CircuitElement.namespaces. Please change the value.');
                    }
                }
                if (property === 'type') {
                    if (!CircuitElement.types.includes(propertyVal)) {
                        throw Error('The "props.type" value must be one of the values in CircuitElement.types. Please change the value');
                    }
                }
                if (property === 'name') {
                    if (propertyVal.includes('.')) {
                        throw Error('The "props.name" property must not contain the "." character. Please change the value.');
                    }
                }
            }
            if (property === 'parent') {
                const ary = propertyVal.split('.');
                if (ary.length !== 2) {
                    throw Error('Invalid value for the "parent" property.');
                }
                const [namespace, name] = ary;
                if (!lettersOrNumbers.test(namespace)) {
                    throw Error(`The namespace component of the "parent" property can only include (1) alphanumeric characters, (2) "-", and (3) "_". Please change the value.`);
                }
                if (!lettersOrNumbers.test(name)) {
                    throw Error(`The name component of the "parent" property can only include (1) alphanumeric characters, (2) "-", and (3) "_". Please change the value.`);
                }
            }
            if (['kw', 'kwh', 'basekv'].includes(property)) {
                if (!onlyNumbers.test(propertyVal)) {
                    throw Error(`"${property}" can only include positive numbers. Please change the value.`);
                }
            }
            if (['singlePhaseLoadCount', 'threePhaseLoadCount'].includes(property)) {
                if (!onlyIntegers.test(propertyVal)) {
                    throw Error(`"${property}" can only include positive integers. Please change the value.`);
                }
            }
            if (['loadProfile'].includes(property)) {
                if (!(propertyVal instanceof Array)) {
                    throw TypeError(`"${property}" must be instance of Array.`);
                }
            }
        } else if (namespace === 'meta') {

        } else {
            throw Error();
        }
    }
}

/**
 * - CSV formatting:
 *  - If rows 1 - 8760 of a column are sequential integers starting at 1 and ending at 8760, then that column represents hours and is not parsed as a
 *    load
 *      - It doesn't matter if the timesteps start at 1 or 0 because they aren't directly passed to OpenDSS
 *  - CSVs are required to have a header row
 */
class CsvLoadParser {
    
    modal;
    #csvLoadsElements;  // - Array of load CircuitElements that is created after a CSV is parsed. They can be added to the model later
    #observers;

    constructor() {
        this.modal = null;
        this.#csvLoadsElements = null;
        this.#observers = [];
        this.renderContent();
    }

    renderContent() {
        const modal = new Modal();
        modal.divElement.id = 'csvLoadParserView';
        const label = document.createElement('label');
        label.classList.add('tooltip');
        label.htmlFor = 'LOAD_CSV';
        label.textContent = 'Load Data (.csv) ';
        const span = document.createElement('span');
        span.classList.add('classic');
        span.textContent = 'Please upload a .csv file representing the hourly load shape. See https://github.com/nreca-bts/microgridup/blob/main/docs/input_formats.md for formatting details.';
        label.append(span);
        modal.divElement.append(label);
        const input = document.createElement('input');
        input.required = true;
        input.type = 'file';
        input.accept = '.csv';
        input.name = 'LOAD_CSV';
        input.classList.add('file-input');
        const that = this;
        input.addEventListener('change', async function() {
            try {
                await that.parseCsv(this.files[0]);
                modal.setBanner('', ['hidden']);
            } catch (e) {
                modal.setBanner(e.message, ['caution']);
            }
        });
        modal.divElement.append(input);
        const button = document.createElement('button');
        button.type = 'button';
        button.classList.add('remove-btn');
        button.textContent = 'Remove File';
        modal.divElement.append(button);
        if (this.modal === null) {
            this.modal = modal;
        }
        if (document.body.contains(this.modal.divElement)) {
            this.modal.divElement.replaceWith(modal.divElement);
            this.modal = modal;
        }
    }

    addObserver(observer) {
        if (this.#observers.includes(observer)) {
            throw Error('The observer is already in this.#observers.');
        } else {
            this.#observers.push(observer);
        }
    }

    /**
     * - Create an array of load objects as a result of parsing the CSV
     * @param {File} file
     * @returns {undefined}
     */
    async parseCsv(file) {
        if (!(file instanceof File)) {
            throw TypeError('"file" argument must be instanceof File.');
        }
        const csvLoadsElements = [];
        const data = await new Promise(function(resolve, reject) {
            Papa.parse(file, {
                complete: function(results, file) {
                    resolve(results);
                },
                dynamicTyping: true,
                error: function(error, file) {
                    reject(error.message);
                },
                header: false,
                skipEmptyLines: true,
            });
        });
        // - omf's microgridDesign model needs exactly one year of hourly data, so every REopt run fails for any other number of rows
        if (data.data.length - 1 !== 8760) {
            throw Error(`CSV parsing failed. The load profile CSV must have exactly 8760 rows of hourly data, not including the required heading row. This file has ${Math.max(data.data.length - 1, 0)}.`);
        }
        const lowercaseHeadings = data.data[0].map(heading => String(heading ?? '').trim().toLowerCase());
        if (new Set(lowercaseHeadings).size !== lowercaseHeadings.length) {
            throw Error('Please ensure that load profile CSV headings are case-insensitive unique.');
        }
        // - Use triangular number formula instead of comparing array contents
        const timeseriesSignature1 = ((8760 ** 2 ) + 8760) / 2;
        const timeseriesSignature2 = ((8759 ** 2 ) + 8759) / 2;
        if (new Set(data.data[0]).size !== data.data[0].length) {
            throw Error('Please ensure the load profile CSV headings are all unique.');
        }
        for (let i = 0; i < data.data[0].length; i++) {
            const loadProfile = [];
            for (let j = 1; j < data.data.length; j++) {
                const kw = data.data[j][i];
                const numKw = +kw;
                if (isNaN(numKw) || kw === null) {
                    throw Error(`CSV parsing failed. Could not parse the value "${kw}" in row "${j}" of column named "${data.data[0][i]}" in file "${file.name}" into a number.`); 
                }
                loadProfile.push(numKw); 
            }
            const sum = loadProfile.reduce((acc, cur) => acc + cur);
            if (sum !== timeseriesSignature1 && sum !== timeseriesSignature2) {
                const element = new CircuitElement({
                    name: data.data[0][i].toString(),
                    namespace: 'element',
                    type: 'load'
                });
                element.setProperty('loadProfile', loadProfile);
                element.setProperty('kw', Math.max(...loadProfile));
                // - When using the manual circuit builder, assume that there is at least one single phase load and one three phase load in the
                //   composite load. This makes debugging easier
                element.setProperty('singlePhaseLoadCount', 1);
                element.setProperty('threePhaseLoadCount', 1);
                csvLoadsElements.push(element);
            }
        }
        this.#csvLoadsElements = csvLoadsElements;
        for (const ob of this.#observers) {
            ob.handleParsedCsv();
        }
    }

    /**
     * @returns {Array}
     */
    getLoads() {
        return this.#csvLoadsElements;
    }
}

class OutageLocationInputView {

    modal;
    #controller;
    #input;

    constructor(circuitController) {
        if (!(circuitController instanceof CircuitController)) {
            throw TypeError('The "circuitController" argument must be instanceof CircuitController.');
        }
        this.modal = null;
        this.#controller = circuitController;
        this.renderContent();
    }

    renderContent() {
        const modal = new Modal();
        const label = document.createElement('label');
        label.classList.add('tooltip');
        label.htmlFor = 'FAULTED_LINES';
        label.textContent = 'Outage Location(s) ';
        const span = document.createElement('span');
        span.classList.add('classic');
        span.textContent = 'Name of the line in the circuit where a typical outage would take place. Use commas to separate multiple lines.';
        label.append(span);
        modal.divElement.append(label);
        const input = document.createElement('input');
        this.#input = input;
        input.id = 'FAULTED_LINES';
        input.name = 'FAULTED_LINES';
        input.pattern = '[\\w\\-]+(?:,[\\w\\-]+)*'
        input.required = true;
        this.updateInput();
        modal.divElement.append(input);
        if (this.modal === null) {
            this.modal = modal;
        }
        if (document.body.contains(this.modal.divElement)) {
            this.modal.divElement.replaceWith(modal.divElement);
            this.modal = modal;
        }
    }

    handleNewElement(element) {
        if (element.getProperty('type') === 'feeder') {
            this.#controller.model.addObserver(this, element.fullName);
            this.renderContent();
        }
    }

    handleRemovedElement(element) {
        if (element.getProperty('type') === 'feeder') {
            this.#controller.model.removeObserver(this, element.fullName);
            this.renderContent();
        }
    }

    handleChangedProperty(element, property, oldPropertyVal, namespace) {
        if (element.getProperty('type') === 'feeder' && property === 'name' && namespace === 'props') {
            this.renderContent();
        }
    }

    setValue(value) {
        if (typeof value !== 'string') {
            throw TypeError('The "value" argument must be typeof "string".');
        }
        this.#input.value = value;
    }

    updateInput() {
        this.#input.value = this.#controller.model.getElements(element => element.getProperty('type') === 'feeder').map(feeder => feeder.getProperty('name')).join(',');
    }
}

class CircuitModel {

    #graph;
    #nameToKey;
    #nextKey;
    #elementObservers;
    #graphObservers;
 
    constructor() {
        const options = {
            // - Do not allow self-loops. A node cannot have a parent-child relationship with itself
            allowSelfLoops: false,
            // - Do not allow multiple edges between nodes. I can't think of a reason to allow it if I don't have to. Although, allowing it would permit
            //   nodes to have multiple types of relationships between them
            multi: false,
            // - The graph must be undirected to accomodate different circuit representations
            type: 'undirected'
        };
        this.#graph = new graphology.Graph(options);
        this.#nameToKey = {};
        this.#nextKey = 0;
        this.#elementObservers = {};
        this.#graphObservers = [];
    }

    addElement(element) {
        if (!(element instanceof CircuitElement)) {
            throw TypeError('The "element" argument must be instance of CircuitElement.');
        }
        this.#addToNameToKey(this.#nextKey.toString(), element.fullName);
        this.#graph.addNode(this.#nextKey.toString(), element);
        if (element.hasProperty('parent')) {
            // - Parent-child edges always go from parent to child
            const sourceKey = this.#getKey(element.getProperty('parent'));
            const targetKey = this.#getKey(element.fullName);
            this.#graph.addEdge(sourceKey, targetKey)
        }
        this.#nextKey++;
        for (const ob of this.#graphObservers) {
            ob.handleNewElement(element);
        }
    }

    addObserver(observer, fullName=null) {
        if (fullName === null) {
            if (this.#graphObservers.includes(observer)) {
                throw Error('The observer is already in this.#graphObservers');
            } else {
                this.#graphObservers.push(observer);
            }
        } else {
            const key = this.#getKey(fullName);
            if (this.#elementObservers[key] instanceof Array) {
                if (this.#elementObservers[key].includes(observer)) {
                    throw Error(`The observer is already in this.#elementObservers[${key}]`);
                } else {
                    this.#elementObservers[key].push(observer);
                }
            } else {
                this.#elementObservers[key] = [observer];
            }
        }
    }

    getElement(fullName) {
        if (typeof fullName !== 'string') {
            throw TypeError('The "fullName" argument must be typeof "string".');
        }
        const key = this.#getKey(fullName);
        return this.#graph.getNodeAttributes(key);
    }

    /**
     * @param {function} func - a function that takes a CircuitElement as an argument and returns true or false
     * @returns {Array} an array of CircuitElements
     */
    getElements(func=null) {
        if (func !== null && typeof func !== 'function') {
            throw TypeError('The "func" argument must be typeof "function".');
        }
        if (func === null) {
            func = () => true;
        }
        return this.#graph.filterNodes((key, element) => {
            return func(element);
        }).map(key => this.#graph.getNodeAttributes(key));
    }

    /**
     * @returns {Array} the direct children of the element, not grandchildren
     */
    getChildElements(fullName) {
        if (typeof fullName !== 'string') {
            throw TypeError('The "fullName" argument must be typeof "string".');
        }
        const key = this.#getKey(fullName);
        const children = [];
        for (const {neighbor, attributes} of this.#graph.neighborEntries(key)) {
            if (attributes.hasProperty('parent') && attributes.getProperty('parent') === fullName) {
                children.push(attributes);
            }
        }
        return children;
    }

    /**
     * - Get all elements that are connected to this element, directly or indirectly
     *  - If other relationships beside parent-child are added later, then this method will need to be updated
     */
    getConnectedElements(fullName) {
        if (typeof fullName !== 'string') {
            throw TypeError('The "fullName" argument must be typeof "string".');
        }
        const visited = [];
        const stack = [this.#getKey(fullName)];
        while (stack.length > 0) {
            const stackKey = stack.pop();
            const element = this.#graph.getNodeAttributes(stackKey);
            if (!visited.includes(element)) {
                visited.push(element); 
                for (const {neighbor, attributes} of this.#graph.neighborEntries(stackKey)) {
                    if (attributes.hasProperty('parent') && attributes.getProperty('parent') === element.fullName) { 
                        stack.push(neighbor);
                    }
                }
            }
        }
        visited.splice(0, 1);
        return visited;
    }
        
    /**
     * @param {string} property - if the element no longer has the property, it means the property was deleted
     * @param {} oldPropertyVal - if this is undefined, it means a new property was just added
     */
    notifyObserversOfChangedProperty(element, property, oldPropertyVal, namespace) {
        const key = this.#getKey(element.fullName);
        if (this.#elementObservers[key] instanceof Array) {
            for (const ob of this.#elementObservers[key]) {
                ob.handleChangedProperty(element, property, oldPropertyVal, namespace);
            }
        }
    }

    /**
     * - Removing an element also removes all of its children (and grandchildren, etc.)
     */
    removeElement(fullName) {
        if (typeof fullName !== 'string') {
            throw TypeError('The "fullName" argument must be typeof "string".');
        }
        const key = this.#getKey(fullName);
        const originalElement = this.#graph.getNodeAttributes(key);
        const connectedElements = this.getConnectedElements(fullName);
        const allElements = [originalElement, ...connectedElements];
        for (const element of allElements) {
            const key = this.#getKey(element.fullName);
            // - Remove the element (and its children) from the graph before updating any displays
            this.#graph.dropNode(key);
            const copy = Array.from(this.#elementObservers[key]);
            for (const ob of copy) {
                // - Must notify each individual observer of an element about that element's removal. When an observer handles the removal, it also
                //   updates its display. Therefore, the model must be updated (i.e. the element must be removed from the model) BEFORE
                //   handleRemovedElement() is called in order for the display to remove the element. "Removed from the model" means removed from the
                //   graph, not removed from this.#nameToKey. removeObserver() requires that the key of the element that's being deleted still exists
                //   in this.#nameToKey, so don't call this.#removeFromNameToKey() until all of the observers have been removed
                ob.handleRemovedElement(element);
            }
            // - Now that the element and all of its children have been removed from the graph, and all the observers have been removed, I can remove
            //   the element's key from this.#nameToKey
            this.#removeFromNameToKey(element.fullName);
            delete this.#elementObservers[key];
        }
    }

    removeObserver(observer, fullName=null) {
        if (fullName === null) {
            const index = this.#graphObservers.indexOf(observer);
            if (index > -1) {
                this.#graphObservers.splice(index, 1);
            } else {
                throw Error(`The observer "${observer}" was not observing the graph.`);
            }
        } else {
            const key = this.#getKey(fullName);
            const index = this.#elementObservers[key].indexOf(observer);
            if (index > -1) {
                this.#elementObservers[key].splice(index, 1);
            } else {
                throw Error(`The observer "${observer}" was not observing the element "${fullName}".`);
            }
        }
    }

    /**
     * @param {string} fullName - the identifying name of the element that is being renamed
     * @param {string} newName - the new name for the element
     * @returns {undefined}
     */
    renameElement(fullName, newName) {
        if (typeof fullName !== 'string') {
            throw TypeError('The "fullName" argument must be typeof "string".');
        }
        if (typeof newName !== 'string') {
            throw TypeError('The "newName" argument must be typeof "string".');
        }
        const [namespace, oldName] = fullName.split('.');
        const newFullName = `${namespace}.${newName}`;
        if (this.#nameToKey.hasOwnProperty(newFullName) && oldName !== newName) {
            throw Error(`The name "${newName}" is already taken by another object.`);
        }
        for (const e of this.getChildElements(fullName)) {
            e.setProperty('parent', newFullName);
        }
        const key = this.#removeFromNameToKey(fullName);
        this.#addToNameToKey(key, newFullName);
        this.getElement(newFullName).setProperty('name', newName);
    }

    // *********************
    // ** Private methods ** 
    // *********************

    #addToNameToKey(key, fullName) {
        if (typeof key !== 'string') {
            throw TypeError('The "key" argument must be typeof "string".');
        }
        if (typeof fullName !== 'string') {
            throw TypeError('The "fullName" argument must be typeof "string".');
        }
        if (!this.#nameToKey.hasOwnProperty(fullName)) {
            this.#nameToKey[fullName] = key;
        } else {
            throw Error(`The name "${fullName.split('.')[1]}" is already taken by another object.`);
        }
    }

    #getKey(fullName) {
        if (this.#nameToKey.hasOwnProperty(fullName)) {
            return this.#nameToKey[fullName];
        } else {
            throw Error(`The name "${fullName.split('.')[1]}" was not found in this.#nameToKey.`);
        }
    }

    #removeFromNameToKey(fullName) {
        if (this.#nameToKey.hasOwnProperty(fullName)) {
            const key = this.#nameToKey[fullName];
            delete this.#nameToKey[fullName];
            return key;
        }
        throw Error(`The name "${fullName.split('.')[1]}" was not found in this.#nameToKey.`);
    }
}

class CircuitController {

    model;

    constructor(model) {
        if (!(model instanceof CircuitModel)) {
            throw TypeError('The "model" argument must be instance of CircuitModel.');
        }
        this.model = model;
    }

    /**
     * - This should probably be called "setElementProperty"
     * @param {} propertyVal - if this is undefined, it means that the property should be deleted from the element
     */
    setProperty(fullName, property, propertyVal, namespace='props') {
        const element = this.model.getElement(fullName);
        let oldPropertyVal = undefined;
        if (element.hasProperty(property, namespace)) {
            oldPropertyVal = element.getProperty(property, namespace);
        }
        CircuitElement.validateProperty(property, propertyVal, namespace);
        if (namespace === 'props' && property === 'name') {
            this.model.renameElement(fullName, propertyVal);
        } else {
            element.setProperty(property, propertyVal, namespace);
        }
        this.model.notifyObserversOfChangedProperty(element, property, oldPropertyVal, namespace);
    }
}

class CircuitUserControlsView {

    modal;                              // - The outermost modal that wraps all of the inner modals
    #controller;                        // - A CircuitController instance
    #circuitElementSelect;              // - A <select> element for choosing the element type to add to the circuit
    #circuitElementNameElement;         // - A <select> or <input> element for choosing the name of the element to add to the circuit
    #circuitElementParentTypeSelect;    // - A <select> element for choosing the type of the parent element of an element to add to the circuit (if needed)
    #circuitElementParentNameSelect;    // - A <select> element for choosing the parent element of the element to add to the circuit (if needed)
    #csvLoadParser;
    #userInputDiv;                      // - A <div> that contains user controls

    constructor(circuitController, csvLoadParser) {
        if (!(circuitController instanceof CircuitController)) {
            throw TypeError('The "circuitController" argument must be instanceof CircuitController.');
        }
        if (!(csvLoadParser instanceof CsvLoadParser)) {
            throw TypeError('The "csvLoadParser" argument must be instanceof CsvLoadParser.');
        }
        this.modal = null;
        this.#controller = circuitController;
        this.#csvLoadParser = csvLoadParser;
        this.#circuitElementSelect = null;
        this.#circuitElementNameElement = null;
        this.#circuitElementParentTypeSelect = null;
        this.#circuitElementParentNameSelect = null;
        this.#userInputDiv = null;
        this.renderContent();
        this.controller = this.#controller;
    }

    /**
     * - Refresh the content without re-creating everything
     * @returns {undefined}
     */
    refreshContent() {
        if (this.#circuitElementParentTypeSelect !== null) {
            const oldVal = this.#circuitElementParentNameSelect.value
            const select = this.#getNewCircuitElementParentNameSelect(this.#circuitElementParentTypeSelect.value);
            for (const op of select.options) {
                if (op.value === oldVal) {
                    op.selected = true;
                }
            }
            this.#circuitElementParentNameSelect.replaceWith(select);
            this.#circuitElementParentNameSelect = select;
            this.#circuitElementParentNameSelect.addEventListener('focus', () => {
                this.modal.setBanner('', ['hidden']);
            });
        }
        if (this.#circuitElementSelect.value === 'load') {
            const oldVal = this.#circuitElementNameElement.value;
            const select = this.#getNewLoadNameSelect();
            for (const op of select.options) {
                if (op.value === oldVal) {
                    op.selected = true;
                }
            }
            this.#circuitElementNameElement.replaceWith(select);
            this.#circuitElementNameElement = select;
            this.#circuitElementNameElement.addEventListener('focus', () => {
                this.modal.setBanner('', ['hidden']);
            });
        }
    }

    /**
     * - Render the modal for the first time
     * @returns {undefined}
     */
    renderContent() {
        const modal = new Modal();
        modal.divElement.classList.add('outermostModal');
        modal.divElement.id = 'circuitUserControlsView';
        this.#createUserInputControls(modal);
        this.#createUserControlsDynamicDiv();
        this.#createAddElementButton(modal);
        this.#createSubmitButton(modal);
        if (this.modal === null) {
            this.modal = modal;
        }
        if (document.body.contains(this.modal.divElement)) {
            this.modal.divElement.replaceWith(modal.divElement);
            this.modal = modal;
        }
    }

    /**
     * - Instead of try-catching in handleNewElement(), I need a different event to listen for. handleNewElement assumes the element is already IN the
     *   model. That's how it works. If I break that assumption I need a new event type
     */
    handleParsedCsv() {
        this.refreshContent();
    }

    handleNewElement(element) {
        this.#controller.model.addObserver(this, element.fullName);
        this.refreshContent();
    }

    handleRemovedElement(element) {
        this.#controller.model.removeObserver(this, element.fullName);
        this.refreshContent();
    }

    handleChangedProperty(element, property, oldPropertyVal, namespace) {
        this.refreshContent();
    }

    // *********************
    // ** Private methods ** 
    // *********************

    /**
     * - Create user input controls for adding a new element
     * @param {Modal} modal
     * @returns {undefined}
     */
    #createUserInputControls(modal) {
        if (!(modal instanceof Modal)) {
            throw TypeError('The "modal "argument must be instanceof Modal');
        }
        this.#userInputDiv = document.createElement('div');
        this.#userInputDiv.classList.add('div--modalUserInputRow');
        modal.divElement.append(this.#userInputDiv);
        const staticDiv = document.createElement('div');
        this.#userInputDiv.append(staticDiv);
        const span = document.createElement('span');
        staticDiv.append(span);
        span.textContent = 'Add new';
        this.#circuitElementSelect = document.createElement('select');
        staticDiv.append(this.#circuitElementSelect);
        const types = ['<select element>', ...CircuitElement.types];
        for (const t of types) {
            const option = document.createElement('option');
            option.label = t;
            option.value = t;
            this.#circuitElementSelect.add(option);
        };
        const dynamicDiv = document.createElement('div');
        this.#userInputDiv.append(dynamicDiv);
        this.#circuitElementSelect.addEventListener('change', () => {
            this.#createUserControlsDynamicDiv();
        });
        // - Error message should disappear when the user clicks on the select
        this.#circuitElementSelect.addEventListener('focus', () => {
            modal.setBanner('', ['hidden']);
        });
    }

    #createUserControlsDynamicDiv() {
        const dynamicDiv = this.#userInputDiv.children[1];
        const circuitElement = this.#circuitElementSelect.value;
        let circuitElementNameElement = null;
        let circuitElementParentTypeSelect = null;
        let circuitElementParentNameSelect = null;
        if (circuitElement === '<select element>') {
            dynamicDiv.replaceChildren();
        } else if (circuitElement === 'substation') {
            const span = document.createElement('span');
            span.textContent = 'named';
            circuitElementNameElement = document.createElement('input');
            dynamicDiv.replaceChildren(span, circuitElementNameElement);
        } else if (circuitElement === 'feeder') {
            const span1 = document.createElement('span');
            span1.textContent = 'named';
            circuitElementNameElement = document.createElement('input');
            const span2 = document.createElement('span');
            span2.textContent = 'to';
            circuitElementParentTypeSelect = document.createElement('select');
            const option = document.createElement('option');
            option.label = 'substation';
            option.value = 'substation';
            circuitElementParentTypeSelect.add(option);
            const span3 = document.createElement('span');
            span3.textContent = 'named';
            circuitElementParentNameSelect = this.#getNewCircuitElementParentNameSelect('substation');
            dynamicDiv.replaceChildren(span1, circuitElementNameElement, span2, circuitElementParentTypeSelect, span3, circuitElementParentNameSelect);
        } else if (circuitElement === 'load') {
            const span1 = document.createElement('span');
            span1.textContent = 'named';
            circuitElementNameElement = this.#getNewLoadNameSelect();
            const span2 = document.createElement('span');
            span2.textContent = 'to';
            circuitElementParentTypeSelect = document.createElement('select');
            const option = document.createElement('option');
            option.label = 'feeder';
            option.value = 'feeder';
            circuitElementParentTypeSelect.add(option);
            const span3 = document.createElement('span');
            span3.textContent = 'named';
            circuitElementParentNameSelect = this.#getNewCircuitElementParentNameSelect('feeder');
            dynamicDiv.replaceChildren(span1, circuitElementNameElement, span2, circuitElementParentTypeSelect, span3, circuitElementParentNameSelect);
        } else {
            const span1 = document.createElement('span');
            span1.textContent = 'named'; 
            circuitElementNameElement = document.createElement('input');
            const span2 = document.createElement('span');
            span2.textContent = 'to';
            circuitElementParentTypeSelect = document.createElement('select');
            const option = document.createElement('option');
            option.label = 'feeder';
            option.value = 'feeder';
            circuitElementParentTypeSelect.add(option);
            const span3 = document.createElement('span');
            span3.textContent = 'named';
            circuitElementParentNameSelect = this.#getNewCircuitElementParentNameSelect('feeder');
            dynamicDiv.replaceChildren(span1, circuitElementNameElement, span2, circuitElementParentTypeSelect, span3, circuitElementParentNameSelect);
        }
        // - Error message should disappear when the user clicks on the select or input
        if (circuitElementNameElement !== null) {
            this.#circuitElementNameElement = circuitElementNameElement;
            this.#circuitElementNameElement.addEventListener('focus', () => {
                this.modal.setBanner('', ['hidden']);
            });
        }
        if (circuitElementParentTypeSelect !== null) {
            this.#circuitElementParentTypeSelect = circuitElementParentTypeSelect;
            this.#circuitElementParentTypeSelect.addEventListener('focus', () => {
                this.modal.setBanner('', ['hidden']);
            });
        }
        if (circuitElementParentNameSelect !== null) {
            this.#circuitElementParentNameSelect = circuitElementParentNameSelect;
            this.#circuitElementParentNameSelect.addEventListener('focus', () => {
                this.modal.setBanner('', ['hidden']);
            });
        }
    }

    /**
     * @param {Modal} modal
     * @returns {undefined}
     */
    #createAddElementButton(modal) {
        if (!(modal instanceof Modal)) {
            throw TypeError('The "modal "argument must be instanceof Modal.');
        }
        const div = document.createElement('div');
        div.classList.add('div--singleButtonDiv');
        const button = document.createElement('button');
        div.append(button);
        button.classList.add('button--modalButton');
        const span = document.createElement('span');
        button.append(span);
        span.textContent = 'Add Element';
        button.addEventListener('click', (e) => {
            // - Don't let form be submitted
            e.preventDefault();
            const circuitElement = this.#circuitElementSelect.value;
            if (circuitElement === '<select element>') {
                // - TODO: setBanner()
                this.modal.setBanner('Please select a circuit element to add.', ['caution']);
                return;
            } else if (circuitElement === 'substation') {
                const substationName = this.#circuitElementNameElement.value.trim();
                try {
                    this.#controller.model.addElement(new CircuitElement({
                        name: substationName,
                        namespace: 'element',
                        type: 'substation',
                        basekv: 2.4
                    }));
                    this.modal.setBanner('', ['hidden']);
                } catch (e) {
                    this.modal.setBanner(e.message, ['caution']);
                    return;
                }
            } else if (circuitElement === 'feeder') {
                const substationName = this.#circuitElementParentNameSelect.value;
                if (substationName === '<select name>') {
                    this.modal.setBanner('Please select a substation name.', ['caution']);
                    return;
                }
                const feederName = this.#circuitElementNameElement.value.trim();
                try {
                    this.#controller.model.addElement(new CircuitElement({
                        name: feederName,
                        namespace: 'element',
                        type: 'feeder',
                        parent: substationName
                    }));
                    this.modal.setBanner('', ['hidden']);
                } catch (e) {
                    this.modal.setBanner(e.message, ['caution']);
                    return;
                }
            } else {
                const childName = this.#circuitElementNameElement.value;
                if (childName === '<select name>') {
                    this.modal.setBanner(`Please select a ${circuitElement} name.`, ['caution']);
                    return;
                }
                let parentType = this.#circuitElementParentTypeSelect.value; // - For now, this is always "feeder"
                if (parentType === '<select element>') {
                    this.modal.setBanner('Please select a circuit element', ['caution']);
                    return;
                }
                const parentName = this.#circuitElementParentNameSelect.value;
                if (parentName === '<select name>') {
                    this.modal.setBanner(`Please select a ${parentType} name`, ['caution']);
                    return
                }
                let childProps = {
                    name: childName,
                    namespace: 'element',
                    parent: parentName   
                };
                if (circuitElement === 'solar') {
                    childProps.type = 'solar';
                    childProps.kw = 440;
                } else if (circuitElement === 'wind') {
                    childProps.type = 'wind';
                    childProps.kw = 200;
                } else if (circuitElement === 'fossil') {
                    childProps.type = 'fossil';
                    childProps.kw = 265;
                } else if (circuitElement === 'battery') {
                    childProps.type = 'battery';
                    childProps.kwh = 307;
                    childProps.kw = 79;
                }
                try {
                    if (circuitElement === 'load') {
                        const load = this.#csvLoadParser.getLoads().filter(load => load.getProperty('name') === childName)[0];
                        const loadPropsCopy = structuredClone(load.getProperties());
                        const loadMetaCopy = structuredClone(load.getProperties('meta'));
                        const loadCopy = new CircuitElement(loadPropsCopy, loadMetaCopy);
                        loadCopy.setProperty('parent', parentName);
                        this.#controller.model.addElement(loadCopy);
                    } else {
                        this.#controller.model.addElement(new CircuitElement(childProps));
                    }
                    this.modal.setBanner('', ['hidden']);
                } catch (e) {
                    this.modal.setBanner(e.message, ['caution']);
                    return;
                }
            }
        });
        modal.divElement.append(div);
    }

    #createSubmitButton(modal) {
        if (!(modal instanceof Modal)) {
            throw TypeError('The "modal "argument must be instanceof Modal.');
        }
        const div = document.createElement('div');
        div.classList.add('div--singleButtonDiv');
        const button = document.createElement('button');
        div.append(button);
        button.classList.add('button--modalButton');
        const span = document.createElement('span');
        button.append(span);
        span.textContent = 'Submit circuit';
        button.addEventListener('click', async (e) => {
            // - Don't let form be submitted
            e.preventDefault();
            // - Check if there are any caution divs. Technically, only the circuit builder names and properties and model name need to be valid
            if ([...document.getElementsByClassName('caution')].length > 0) {
                alert('Please correct any erroneous inputs with a highlighted error message before submitting your circuit.');
                return;
            }
            const formData = new FormData();
            const modelNameInput = document.querySelector('input[name="MODEL_DIR"]');
            formData.append('MODEL_DIR', modelNameInput.value.trim());
            const latitudeInput = document.querySelector('input[name="latitude"]');
            formData.append('latitude', latitudeInput.value.trim());
            const longitudeInput = document.querySelector('input[name="longitude"]');
            formData.append('longitude', longitudeInput.value.trim());
            const ary = [];
            for (const e of this.#controller.model.getElements(e => true)) {
                const element = structuredClone(e.getProperties());
                if (e.hasProperty('parent')) {
                    const parentName = e.getProperty('parent').split('.')[1];
                    element['parent'] = parentName;
                }
                ary.push(element);
            }
            formData.append('json', JSON.stringify(ary));
            // Recognize if we're on the edit flow. If we are not, backend should search data/projects for model_dir and return error if it already exists.
            formData.append('onEditFlow',window.onEditFlow);
            const that = this;
            try {
                const response = await fetch('/wizard_to_dss', {
                    method: 'POST',
                    body: formData
                });
                const responseJson = await response.json();
                if (!response.ok) {
                    throw new Error(responseJson.message);
                }
                // Enable partition selector.
				document.getElementById('previewPartitionsButton').disabled = false;
				document.getElementById('partitionMethod').disabled = false;
				window.circuitIsSpecified = true; // - Global variable!
				const loads = responseJson.loads;
                // Clear critLoads.
				const critLoadsContainer = document.getElementById('critLoads');
				critLoadsContainer.innerHTML = '';
                // Instructions for critLoads.
				const critLoadsParagraph = document.createElement('p');
				critLoadsParagraph.textContent = 'Please select all critical loads:';
				critLoadsContainer.appendChild(critLoadsParagraph);
                // Clear previous content and add loads to manualpartitioningcontainer (shown if user chooses manual partitioning).
				const manualPartitioningContainer = document.getElementById('manualPartitioningContainer');
				manualPartitioningContainer.innerHTML = '';
				manualPartitioningContainer.style.display = 'none';
                // Form element for checkboxes.
				const criticalLoadsSelect = document.createElement('form');
				criticalLoadsSelect.id = 'criticalLoadsSelect';
				criticalLoadsSelect.classList.add('chunk');
				critLoadsContainer.appendChild(criticalLoadsSelect);
                // Create a div to house load assignment dropdown menus.
				const loadAssignmentContainer = document.createElement('div');
				loadAssignmentContainer.id = 'loadAssignmentContainer';
				manualPartitioningContainer.appendChild(loadAssignmentContainer);
                // Create checkboxes and labels for each load.
				for (let i = 0; i < loads.length; i++) {
					const label = document.createElement('label');
					label.style.display = 'inline-block';
					label.style.paddingRight = '5px';
					const checkbox = document.createElement('input');
					checkbox.type = 'checkbox';
					label.appendChild(checkbox);
					label.appendChild(document.createTextNode(loads[i]));
					criticalLoadsSelect.appendChild(label);
					// Add a dropdown to loadAssignmentContainer.
					const dropdownLabel = document.createElement('label');
					dropdownLabel.style.display = 'inline-block';
					dropdownLabel.style.paddingRight = '8px';
					const dropdown = document.createElement('select');
					dropdown.style.marginRight = '3px';
					dropdown.dataset.loadId = loads[i];
					dropdownLabel.appendChild(dropdown);
					dropdownLabel.appendChild(document.createTextNode(loads[i]));
					loadAssignmentContainer.appendChild(dropdownLabel);
				}
                // Handle the case where there are no loads.
				if (loads.length === 0) {
					const noLoadsMessage1 = document.createElement('p');
					noLoadsMessage1.textContent = 'No loads to select from.';
					criticalLoadsSelect.appendChild(noLoadsMessage1);

					const noLoadsMessage2 = document.createElement('p');
					noLoadsMessage2.textContent = 'No loads to assign to microgrids.';
					manualPartitioningContainer.appendChild(noLoadsMessage2);
				}
                window.filename = responseJson.filename; // Set filename. // Global variable!
                const modelDirInput = document.querySelector('input[name="MODEL_DIR"]');
				if (modelDirInput) {
					modelDirInput.readOnly = true; // Make the directory input uneditable.
				}
                that.modal.setBanner('', ['hidden']);
                document.getElementById('partitionMethod').dispatchEvent(new Event('change')); // Manually trigger dropdown select menu change to potentially refresh partition method descriptions (manual partitioning description differs dependent on wizard vs upload).
            } catch (error) {
                that.modal.setBanner(data.responseJSON.error, ['caution']);
                setTimeout(() => {
                    that.modal.setBanner('', ['hidden']);
                }, 5000)
                alert(error);
            }
        });
        modal.divElement.append(div);
    }

    #getNewCircuitElementParentNameSelect(type) {
        if (typeof type !== 'string') {
            throw TypeError('The "type" argument must be typeof "string"');
        }
        const select = document.createElement('select');
        const option = document.createElement('option');
        option.label = '<select name>'
        option.value = '<select name>'
        select.add(option);
        const elements = this.#controller.model.getElements(e => e.getProperty('type') == type);
        for (const e of elements) {
            const option = document.createElement('option');
            option.label = `${e.getProperty('name')}`;
            option.value = e.fullName;
            select.add(option);
        }
        return select;
    }

    #getNewLoadNameSelect() {
        const select = document.createElement('select');
        const option = document.createElement('option');
        option.label = '<select name>';
        option.value = '<select name>';
        select.add(option);
        const loads = this.#csvLoadParser.getLoads();
        if (loads !== null) {
            for (const load of loads) {
                const option = document.createElement('option');
                option.label = `${load.getProperty('name')}`;
                option.value = `${load.getProperty('name')}`;
                select.add(option);
            }
        }
        return select;
    }
}

class CircuitTableView {

    modal;          // - The outermost modal that wraps all of the inner modals
    #controller;    // - A CircuitController instance

    constructor(circuitController) {
        if (!(circuitController instanceof CircuitController)) {
            throw TypeError('The "circuitController" argument must be instanceof CircuitController.');
        }
        this.modal = null;
        this.#controller = circuitController;
        this.renderContent();
    }

    /**
     * - Render the modal for the first time
     * @returns {undefined}
     */
    renderContent() {
        const modal = new Modal();
        modal.divElement.id = 'circuitTableView';
        modal.setTitle('Circuit Builder');
        const substationElements = this.#controller.model.getElements(element => {
            return element.getProperty('type') === 'substation';
        });
        for (const substationElement of substationElements) {
            const substationModal = new Modal();
            substationModal.divElement.classList.add('indent-level-0');
            const row = this.#getElementRow(substationElement);
            substationModal.insertTBodyRow(row);
            modal.divElement.append(substationModal.divElement);
            this.#insertPropertiesModal(modal, substationElement, ['indent-level-3']);
            // - For now, only feeders should be children of substations
            for (const feederElement of this.#controller.model.getChildElements(substationElement.fullName)) {
                const feederModal = new Modal();
                feederModal.divElement.classList.add('indent-level-1');
                const row = this.#getElementRow(feederElement);
                feederModal.insertTBodyRow(row);
                modal.divElement.append(feederModal.divElement);
                // - No feeder properties may be set by users
                //this.#insertPropertiesModal(modal, feederElement, ['indent-level-3']);
                // - For now, only feeders should have children (e.g. loads don't have children)
                for (const childElement of this.#controller.model.getChildElements(feederElement.fullName)) {
                    const childModal = new Modal();
                    childModal.divElement.classList.add('indent-level-2');
                    const row = this.#getElementRow(childElement);
                    childModal.insertTBodyRow(row);
                    modal.divElement.append(childModal.divElement);
                    //if (!['load'].includes(childElement.getProperty('type'))) {
                        this.#insertPropertiesModal(modal, childElement, ['indent-level-3']);
                    //}
                }
            }
        }
        if (this.modal === null) {
            this.modal = modal;
        }
        if (document.body.contains(this.modal.divElement)) {
            this.modal.divElement.replaceWith(modal.divElement);
            this.modal = modal;
        }
    }

    handleNewElement(element) {
        this.#controller.model.addObserver(this, element.fullName);
        this.renderContent();
    }

    handleRemovedElement(element) {
        this.#controller.model.removeObserver(this, element.fullName);
        this.renderContent();
    }

    handleChangedProperty(element, property, oldPropertyVal, namespace) {
        // - Don't re-render the table because then all of the error messages will go away
        //this.renderContent();
    }

    // *********************
    // ** Private methods ** 
    // *********************

    #getElementRow(element) {
        if (!(element instanceof CircuitElement)) {
            throw TypeError('The "element" argument must be instanceof CircuitElement.');
        }
        const button = document.createElement('button');
        button.classList.add('delete');
        button.append(getTrashCanSvg());
        button.addEventListener('click', () => {
            this.#controller.model.removeElement(element.fullName);
        });
        const circuitElementSpan = document.createElement('span');
        const type = element.getProperty('type');
        circuitElementSpan.textContent = type;
        const errorElement = document.createElement('div');
        const name = element.getProperty('name');
        let circuitElementNameElement;
        if (type === 'load') {
            circuitElementNameElement = document.createElement('span');
            circuitElementNameElement.textContent = name;
        } else {
            circuitElementNameElement = document.createElement('input');
            const that = this;
            circuitElementNameElement.addEventListener('change', function() {
                const newName = this.value.trim();
                try {
                    that.#controller.setProperty(element.fullName, 'name', newName);
                    errorElement.textContent = '';
                    errorElement.classList.remove('caution');
                    //that.modal.setBanner('', ['hidden']);
                } catch (e) {
                    errorElement.textContent = e.message;
                    errorElement.classList.add('caution');
                    //this.value = that.#controller.model.getElement(element.fullName).getProperty('name');
                    //that.modal.setBanner(e.message, ['caution']);
                }
            });
            circuitElementNameElement.value = name;
        }
        return [button, circuitElementSpan, circuitElementNameElement, errorElement];
    }

    #getElementPropertiesRows(element) {
        const doNotDisplayAsProperties = ['namespace', 'name', 'type', 'parent'];
        const readOnly = ['loadProfile'];
        const rows = [];
        const that = this;
        for (const [key, val] of Object.entries(element.getProperties())) {
            if (!doNotDisplayAsProperties.includes(key)) {
                //const button = document.createElement('button');
                //button.append(getTrashCanSvg());
                //button.addEventListener('click', () => {
                //    this.#controller.setProperty(fullName, key, undefined)
                //});
                const propertySpan = document.createElement('span'); 
                propertySpan.textContent = key;
                const errorElement = document.createElement('div');
                const valueInput = document.createElement('input');
                valueInput.value = val;
                valueInput.addEventListener('change', function() {
                    try {
                        that.#controller.setProperty(element.fullName, key, this.value);
                        errorElement.textContent = '';
                        errorElement.classList.remove('caution');
                        //that.modal.setBanner('', ['hidden']);
                    } catch (e) {
                        errorElement.textContent = e.message;
                        errorElement.classList.add('caution');
                        //this.value = that.#controller.model.getElement(element.fullName).getProperty(key);
                        //that.modal.setBanner(e.message, ['caution']);
                    }

                });
                if (readOnly.includes(key)) {
                    valueInput.disabled = true;
                }
                if (element.getProperty('type') === 'load' && ['kw'].includes(key)) {
                    valueInput.disabled = true;
                }
                rows.push([null, propertySpan, valueInput, errorElement]);
            }
        }
        return rows;
    }

    #insertPropertiesModal(modal, element, cssClasses) {
        const propertiesModal = new Modal();
        propertiesModal.divElement.classList.add(...cssClasses);
        for (const row of this.#getElementPropertiesRows(element)) {
            propertiesModal.insertTBodyRow(row);
        }
        modal.divElement.append(propertiesModal.divElement);
    }
}

function main() {
    test();
}

function test() {
    let circuitElements = [
        {
            name: 'substation_1',
            namespace: 'element',
            type: 'substation',
        },
        {
            name: 'feeder_1',
            namespace: 'element',
            type: 'feeder',
            parent: 'element.substation_1',
        },
        {
            name: 'solar_1',
            namespace: 'element',
            type: 'solar',
            parent: 'element.feeder_1',
            kw: 440
        },
        {
            name: 'wind_1',
            namespace: 'element',
            type: 'wind',
            parent: 'element.feeder_1',
            kw: 200
        },
        {
            name: 'fossil_1',
            namespace: 'element',
            type: 'fossil',
            parent: 'element.feeder_1',
            kw: 265
        },
        {
            name: 'battery_1',
            namespace: 'element',
            type: 'battery',
            parent: 'element.feeder_1',
            kwh: 307,
            kw: 79
        },
    ];
    circuitElements = circuitElements.map(element => new CircuitElement(element));
    const model = new CircuitModel();
    for (const element of circuitElements) {
        model.addElement(element);
    }
    const controller = new CircuitController(model);
    const table = new CircuitTableView(controller);
    // - Table needs to handleNewElement
    model.addObserver(table);

    const csvLoadParser = new CsvLoadParser();
    const controls = new CircuitUserControlsView(controller, csvLoadParser);
    csvLoadParser.addObserver(controls);
    // - Controls needs to handleNewElement
    model.addObserver(controls);

    for (const element of circuitElements) {
        model.addObserver(table, element.fullName);
        model.addObserver(controls, element.fullName);
    }

    document.body.append(csvLoadParser.modal.divElement);
    document.body.append(table.modal.divElement);
    document.body.append(controls.modal.divElement);

    //document.querySelectorAll('input').forEach(e => e.disabled = true);
    //document.querySelectorAll('select').forEach(e => e.disabled = true);
    //document.querySelectorAll('button').forEach(e => e.disabled = true);
}