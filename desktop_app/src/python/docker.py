#!/usr/bin/env python


import platform, subprocess, time


class Docker:
    '''
    - A class to initialize the Docker environment
    '''

    def __init__(self, progress_callback=print):
        '''
        :param progress_callback: the function to report progress (e.g. print() or src.python.WorkerSignals.progress.emit())
        :type progress_callback: function
        '''
        self.progress_callback = progress_callback
        # - Determine the appropriate commands based on the operating system
        if platform.system() == 'Darwin':
            self.docker_desktop_cmd = 'open -a Docker'
            self.docker_cli_cmd = '/usr/local/bin/docker'
        elif platform.system() == 'Linux':
            self.docker_desktop_cmd = None
            self.docker_cli_cmd = None
        elif platform.system() == 'Windows':
            self.docker_desktop_cmd = '"C:/Program Files/Docker/Docker/Docker Desktop.exe"'
            self.docker_cli_cmd = '"C:/Program Files/Docker/Docker/resources/bin/docker.exe"'
        else:
            self.progress_callback('The MicrogridUp app could not determine the operating system.')
            raise subprocess.CalledProcessError(1, 'platform.system()')

    def initialize_docker(self):
        '''
        Start the mgu-container

        :return: the ID of the started container
        :rtype: str
        '''
        # - Start Docker Desktop
        #   - If Docker is already running, this command won't do anything
        #   - Installing Docker Desktop makes the $ docker $ command available, but ONLY after Docker Desktop has been opened and configured by the user
        try:
            self.progress_callback('Starting Docker Desktop...')
            self.start_subprocess(f'{self.docker_desktop_cmd}')
        except subprocess.CalledProcessError as e:
            self.progress_callback(f'The MicrogridUp app could not start the Docker Desktop application via the cmd command $ {self.docker_desktop_cmd}. ' +
                'If Docker Desktop is not installed, please install and configure Docker Desktop from https://docs.docker.com/desktop/.')
            raise e
        time.sleep(5)
        # - Pull the image
        try:
            self.progress_callback('Checking for the latest MicrogridUp image...')
            self.start_subprocess(f'{self.docker_cli_cmd} pull ghcr.io/nreca-bts/microgridup:main')
        except subprocess.CalledProcessError as e:
            self.progress_callback('The MicrogridUp app could not pull the ghcr.io/nreca-bts/microgridup:main Docker image. Check that Docker Desktop is working properly, then restart the app.')
            raise e
        # - Check if the Docker volume already exists
        #   - If it does exist, we don't need to do anything
        try:
            self.progress_callback('Checking for the mgu-volume volume...')
            self.start_subprocess(f'{self.docker_cli_cmd} volume inspect mgu-volume')
        except subprocess.CalledProcessError as e:
            # - If the volume does not exist, create it and pre-populate it with data
            try:
                self.progress_callback('Creating the mgu-volume volume...')
                self.start_subprocess(f'{self.docker_cli_cmd} volume create mgu-volume')
            except subprocess.CalledProcessError as e:
                self.progress_callback('The MicrogridUp app could not create a persistent volume for the container.')
                raise e
            try:
                self.progress_callback('Filling the mgu-volume volume with examples...')
                self.start_subprocess(f'{self.docker_cli_cmd} run --rm --mount type=volume,src="mgu-volume",dst="/mgu-volume" --entrypoint bash ghcr.io/nreca-bts/microgridup:main "-c" "cp -r /data/projects/* /mgu-volume/"')
            except subprocess.CalledProcessError as e:
                self.progress_callback('The MicrogridUp app could not copy data from the Docker image at data/projects into the persistent volume mounted at data/projects.')
                raise e
        # - Start the container in detached mode with the persistent volume
        start_container_cmd = f'{self.docker_cli_cmd} run -d --rm -p 5000:5000 --name mgu-container --mount type=volume,source="mgu-volume",target="/data/projects" ghcr.io/nreca-bts/microgridup:main'
        try:
            self.progress_callback('Starting the mgu-container on port 5000...')
            # - We want to block until we get the stdout
            p = subprocess.run(start_container_cmd, shell=True, check=True, capture_output=True, encoding='utf-8')
            self.progress_callback('    ' + p.stderr)
            container_id = p.stdout
        except subprocess.CalledProcessError as e:
            # - If there was an error, try the following:
            try:
                # - Check if the container is still running from a previous run and shut it down
                self.progress_callback('There was a problem starting the mgu-container. Trying to restart the container...')
                self.start_subprocess(f'{self.docker_cli_cmd} stop mgu-container')
                time.sleep(10)
                # - Try to start it again
                p = subprocess.run(start_container_cmd, shell=True, check=True, capture_output=True, encoding='utf-8')
                self.progress_callback('    ' + p.stderr)
                container_id = p.stdout
            except subprocess.CalledProcessError as e:
                # - If we still failed, then port 5000 probably isn't available
                self.progress_callback('The MicrogridUp app could not start the Docker container and bind the web server on port 5000. Please check that port 5000 is open on your local machine.')
                raise e
        # - Wait for the web server to start
        self.progress_callback('Waiting for the web server in the mgu-container to start up...')
        time.sleep(50)
        return container_id

    def stop_docker(self, container_id):
        '''
        Stop the mgu-container

        :param container_id: the ID of the container to shut down
        :type b: str
        :rtype: None
        '''
        try:
            self.progress_callback('Stopping the mgu-container...')
            self.start_subprocess(f'{self.docker_cli_cmd} stop {container_id}')
        except subprocess.CalledProcessError as e:
            self.progress_callback('The MicrogridUp app could not stop the Docker container.')
            raise e

    def start_subprocess(self, cmd):
        '''
        Convenience function for calling subprocess.Popen(), monitoring the stdout and stderr, and raising an error if the subprocess fails
        '''
        with subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as p:
            while p.poll() is None:
                stdout = p.stdout.read1().decode('utf-8')
                if len(stdout) > 0:
                    self.progress_callback('        ' + stdout)
                stderr = p.stderr.read1().decode('utf-8')
                if len(stderr) > 0:
                    self.progress_callback('        ' + stderr)
            if p.returncode != 0:
                raise subprocess.CalledProcessError(p.returncode, cmd)


def _test():
    d = Docker()
    container_id = d.initialize_docker()
    d.stop_docker(container_id)


if __name__ == '__main__':
    _test()
