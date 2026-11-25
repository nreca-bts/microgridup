'''
Drive the MicrogridUP GUI in a real Chrome browser the way a user would. For every project in testfiles/test_params.json, the test fills in the
New page (project name, load data, circuit, critical loads, outage location, partition method), clicks Run Project and waits for the run to finish.
It then checks that the files the GUI left in uploads/ match the back-end test files in testfiles/.

Usage
    python tests_frontend.py                          # test the server at http://localhost:5000, the port that the Docker image serves
    python tests_frontend.py http://localhost:5001    # test a server at another address

The one optional argument is the address of the server, and it defaults to http://localhost:5000. The server must already be running from this
folder, because the test reads the uploads/ folder next to this file. Every project is a full model run, so the whole test takes a long time.
'''
import json, time, filecmp, pathlib, sys
from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select, WebDriverWait


# - The folder that holds this file, testfiles/ and uploads/. It is found from this file's own path so that the test doesn't have to import
#   microgridup, which lets it run on a machine that only has a browser while the server runs somewhere else, e.g. in a Docker container
MGU_DIR = pathlib.Path(__file__).resolve(True).parent
BASE_URL = 'http://localhost:5000'
# - How long to wait for the page to react to a click, and how long to wait for one model run to finish
PAGE_TIMEOUT_SECONDS = 60
RUN_TIMEOUT_SECONDS = 3600
LEHIGH_DSS_PATH = MGU_DIR / 'testfiles' / 'lehigh_base_phased.dss'
WIZARD_DSS_PATH = MGU_DIR / 'testfiles' / 'wizard_base_3mg.dss'
LOAD_CSV_PATH = MGU_DIR / 'testfiles' / 'lehigh_load.csv'


def test_browser_gui():
    options = Options()
    driver = webdriver.Chrome(
        service=ChromeService(ChromeDriverManager().install()),
        options = options
    )
    with open(MGU_DIR / 'testfiles' / 'test_params.json') as file:
        test_params = json.load(file)
    crit_loads = test_params['crit_loads']
    MICROGRIDS = test_params['MICROGRIDS']
    algo_params = test_params['algo_params']
    elements = test_params['elements']
    for _dir in MICROGRIDS:
        print(f'---------------------------------------------------------\nBeginning end-to-end test of {_dir}.\n---------------------------------------------------------')
        # - Open the New page from the Home page, like a user
        driver.get(BASE_URL)
        driver.find_element(By.XPATH, '//a[@href="/new"]').click()
        _wait_for(driver, lambda d: d.execute_script('return window.outageLocationInputView !== undefined && document.readyState === "complete";'))

        MODEL_DIR = driver.find_element(By.NAME, 'MODEL_DIR')
        MODEL_DIR.clear()
        # - Leave the input with the Tab key so that the page checks the name now. The check redraws the input, and a redraw during a later click
        #   would move the page under the mouse
        MODEL_DIR.send_keys(f'{_dir}', Keys.TAB)

        # - Give the page a moment to parse the year of load data before the circuit is specified
        driver.find_element(By.NAME, 'LOAD_CSV').send_keys(str(LOAD_CSV_PATH))
        time.sleep(2)

        used_wizard = 'wizard' in _dir
        if used_wizard:
            _build_circuit_manually(driver, elements)
            # - A circuit that is built on the page lists its feeders as outage locations. Fault all of them at once, which takes the text input
            feeders = [element['name'] for element in elements if element['type'] == 'feeder']
            _choose_outage_locations(driver, feeders)
        else:
            _upload_circuit(driver, LEHIGH_DSS_PATH)
            # - An uploaded circuit lists the first line on each path out of the substation, which is 650632 for the Lehigh circuit
            _choose_outage_locations(driver, ['650632'])

        for load in crit_loads:
            driver.find_element(By.XPATH, f'//form[@id="criticalLoadsSelect"]/label[normalize-space()="{load}"]/input').click()

        algo = MICROGRIDS[_dir][1]
        select = Select(driver.find_element(By.ID, 'partitionMethod'))
        select.select_by_value(algo)

        if algo in ('loadGrouping','manual'):
            _partition_by_hand(driver, algo, algo_params, used_wizard)
        elif algo in ('bottomUp','criticalLoads'):
            driver.find_element(By.ID, 'minQuantMgs').send_keys('3')
            driver.find_element(By.ID, 'previewPartitionsButton').click()
            _wait_for(driver, lambda d: d.find_elements(By.ID, 'previewIframe'))
        else:
            driver.find_element(By.ID, 'previewPartitionsButton').click()
            _wait_for(driver, lambda d: d.find_elements(By.ID, 'previewIframe'))

        # - 'solar', 'battery' and 'fossil' are checked by default when loading in static/lehigh_3mg_inputs.json, so only make sure of it
        technologies = ['solar', 'battery', 'fossil']
        for tech in technologies:
            checkbox = driver.find_element(By.NAME, f'{tech}')
            if not checkbox.is_selected():
                checkbox.click()

        driver.find_element(By.ID, 'submitEverything').click()
        _wait_for_run_to_finish(driver, _dir)
    driver.quit()
    return


def _wait_for(driver, condition, timeout=PAGE_TIMEOUT_SECONDS):
    '''
    Wait until the condition holds. Selenium raises a TimeoutException if it never does, and an UnexpectedAlertPresentException if the page shows
    an alert meanwhile, which is how the GUI reports a rejected circuit or a rejected run

    :param driver: the browser
    :type driver: selenium.webdriver.Chrome
    :param condition: a function that takes the driver and returns something truthy once the wait is over
    :type condition: function
    :param timeout: how many seconds to wait
    :type timeout: int
    :return: what the condition returned
    '''
    return WebDriverWait(driver, timeout).until(condition)


def _upload_circuit(driver, dss_path):
    '''
    Choose "Upload circuit from file", upload the circuit, and wait for the critical loads list that the page shows once the server accepts it

    :param driver: the browser, on the New page
    :type driver: selenium.webdriver.Chrome
    :param dss_path: the circuit file to upload
    :type dss_path: pathlib.Path
    :rtype: None
    '''
    driver.find_element(By.ID, 'upload').click()
    driver.find_element(By.NAME, 'BASE_DSS_NAME').send_keys(str(dss_path))
    driver.find_element(By.ID, 'upload-file-btn').click()
    _wait_for(driver, lambda d: d.find_elements(By.CSS_SELECTOR, '#criticalLoadsSelect label'))


def _build_circuit_manually(driver, elements):
    '''
    Choose "Build circuit manually", replace the sample circuit with the test circuit, press "Submit circuit", and wait for the critical loads list
    that the page shows once the server accepts the circuit

    :param driver: the browser, on the New page
    :type driver: selenium.webdriver.Chrome
    :param elements: the circuit elements as they are posted to /wizard_to_dss, where a parent is a plain name
    :type elements: list
    :rtype: None
    '''
    driver.find_element(By.ID, 'wizard').click()
    # - JavaScript shortcut. Removing a substation also removes everything below it, and the circuit builder stores a parent with its namespace
    driver.execute_script('''
        for (const e of window.circuitModel.getElements(e => e.getProperty('type') === 'substation')) {
            window.circuitModel.removeElement(e.fullName);
        }
        for (const properties of arguments[0]) {
            const element = {...properties, namespace: 'element'};
            if (element.parent !== undefined) {
                element.parent = `element.${element.parent}`;
            }
            // - The circuit builder gives every load that comes from the load CSV these two counts, and a model run needs them to price the relays
            if (element.type === 'load') {
                element.singlePhaseLoadCount = 1;
                element.threePhaseLoadCount = 1;
            }
            window.circuitModel.addElement(new window.CircuitElement(element));
        }
    ''', elements)
    # TO DO: Fill out circuit wizard with Selenium rather than using a JavaScript shortcut.
    driver.find_element(By.XPATH, '//button[span[text()="Submit circuit"]]').click()
    _wait_for(driver, lambda d: d.find_elements(By.CSS_SELECTOR, '#criticalLoadsSelect label'))


def _choose_outage_locations(driver, lines):
    '''
    Set the Outage Location(s) dropdown. One line that the dropdown lists is chosen from the list. Anything else is typed into the text input that
    "Other lines..." reveals

    :param driver: the browser, on the New page with a circuit specified
    :type driver: selenium.webdriver.Chrome
    :param lines: the names of the lines to fault
    :type lines: list
    :rtype: None
    '''
    select = Select(driver.find_element(By.ID, 'outageLocationSelect'))
    listed_lines = [option.text for option in select.options]
    if len(lines) == 1 and lines[0] in listed_lines:
        select.select_by_visible_text(lines[0])
    else:
        select.select_by_visible_text('Other lines...')
        # - The text input slides into view, so wait until it can be typed into
        text_input = driver.find_element(By.ID, 'FAULTED_LINES')
        _wait_for(driver, lambda d: text_input.is_displayed())
        time.sleep(1)
        text_input.send_keys(','.join(lines))


def _partition_by_hand(driver, algo, algo_params, used_wizard):
    '''
    Fill in the Load Grouping or Manual partition method. Ask for three microgrids, tick each microgrid's loads, and for Manual also name each
    microgrid's existing generation and, for an uploaded circuit, its generation bus and switch

    :param driver: the browser, on the New page with a circuit specified and the partition method selected
    :type driver: selenium.webdriver.Chrome
    :param algo: the partition method, 'loadGrouping' or 'manual'
    :type algo: str
    :param algo_params: the "algo_params" of testfiles/test_params.json
    :type algo_params: dict
    :param used_wizard: whether the circuit was built on the page instead of uploaded
    :type used_wizard: bool
    :rtype: None
    '''
    driver.find_element(By.ID, 'mgQuantity').send_keys('3')
    driver.find_element(By.ID, 'makeManuallyPartitioningContainerButton').click()
    _wait_for(driver, lambda d: d.find_elements(By.CSS_SELECTOR, '#loadAssignmentContainer .multiSelectDropdown'))
    # - Each microgrid has a dropdown of checkboxes, one checkbox per load. The loads under "None" stay out of every microgrid
    for mg_id, loads in algo_params['pairings'].items():
        if mg_id == 'None':
            continue
        dropdown = driver.find_element(By.CSS_SELECTOR, f'#loadAssignmentContainer .multiSelectDropdown[data-mg-id="{mg_id}"]')
        dropdown.find_element(By.CLASS_NAME, 'multiSelectToggle').click()
        for load in loads:
            dropdown.find_element(By.CSS_SELECTOR, f'input[data-load-id="{load}"]').click()
    if algo == 'manual':
        if used_wizard:
            existing_objects = WIZARD_DSS_PATH.read_text().lower()
        else:
            existing_objects = '\n'.join(line for line in LEHIGH_DSS_PATH.read_text().lower().splitlines() if not line.strip().startswith('!'))
        for mg_id in algo_params['gen_obs_existing']:
            # - The test parameters serve both circuits, so only name the existing generation that this circuit really has
            gen_obs_existing = [name for name in algo_params['gen_obs_existing'][mg_id].split(',') if name != '' and f'.{name.lower()} ' in existing_objects]
            driver.find_element(By.ID, f'{mg_id}GenObsExisting').send_keys(','.join(gen_obs_existing))
            if not used_wizard:
                # - Fill out switches and gen buses. A circuit that is built on the page has them chosen for it
                driver.find_element(By.ID, f'{mg_id}GenBus').send_keys(algo_params['gen_bus'][mg_id])
                driver.find_element(By.ID, f'{mg_id}Switch').send_keys(algo_params['switch'][mg_id][0])


def _wait_for_run_to_finish(driver, project):
    '''
    Wait for the model run that "Run Project" started. The page moves to /load/<project> once the server accepts the run, and that address
    redirects to the project's output page when the run is finished. Raise an Exception if the run crashes

    :param driver: the browser, right after the click on "Run Project"
    :type driver: selenium.webdriver.Chrome
    :param project: the project name
    :type project: str
    :rtype: None
    '''
    _wait_for(driver, lambda d: f'/load/{project}' in d.current_url or 'output_final.html' in d.current_url)
    deadline = time.time() + RUN_TIMEOUT_SECONDS
    while 'output_final.html' not in driver.current_url:
        # - The in-progress page changes its title to "Project Crashed" when the run has crashed
        if 'crashed' in driver.title.lower():
            raise Exception(f'The model run of "{project}" crashed. See data/projects/{project}/logs.log.')
        if time.time() > deadline:
            raise Exception(f'The model run of "{project}" did not finish within {RUN_TIMEOUT_SECONDS} seconds.')
        time.sleep(15)
        driver.get(f'{BASE_URL}/load/{project}')


def test_browser_gui_output():
    '''
    - Test that the automated selenium tests are creating the output that is expected by the back-end tests in microgridup.py. If there is a file
      mismatch between the output generated by selenium and a file in testfiles/, then either selenium isn't working properly or the files in
      testfiles/ need to be updated
    '''
    filecmp.clear_cache()
    mismatched_files = []
    for child in (MGU_DIR / 'uploads').iterdir():
        if str(child.stem).lower().find('load_csv') > -1:
            if not filecmp.cmp(LOAD_CSV_PATH, child):
                mismatched_files.append((str(child.stem), LOAD_CSV_PATH.name))
        elif str(child.stem).lower().find('lehigh') > -1:
            if not filecmp.cmp(LEHIGH_DSS_PATH, child):
                mismatched_files.append((str(child.stem), LEHIGH_DSS_PATH.name))
        elif str(child.stem).lower().find('wizard') > -1:
            # - wizard_base_3mg.dss has a generic circuit name while tests_frontend.py outputs DSS files with specific circuit names. I have to delete
            #   the circuit name to compare the rest of the files
            with open(child) as f:
                child_contents = f.readlines()
            child_contents.pop(2)
            with open(WIZARD_DSS_PATH) as f:
                dss_contents = f.readlines()
            dss_contents.pop(2)
            if not child_contents == dss_contents:
                mismatched_files.append((str(child.stem), WIZARD_DSS_PATH.name))
        else:
            raise Exception(f'Unrecognized file in uploads/ directory: {str(child.stem)}')
    if len(mismatched_files) > 0:
        for mismatch in mismatched_files:
            print(f'The file "{mismatch[0]}" was expected to match "{mismatch[1]}", but the file contents were different.')
        sys.exit(1) # trigger failure
    print('All files output in uploads/ by tests_frontend.py matched the expected content and format of the back-end test input files.')


if __name__ == '__main__':
    if len(sys.argv) > 1:
        BASE_URL = sys.argv[1].rstrip('/')
    test_browser_gui()
    test_browser_gui_output()
