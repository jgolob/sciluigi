import luigi
import sciluigi
import json
import logging
import subprocess
import docker
import os
from string import Template
import shlex
import uuid
import time
try:
    from urlparse import urlsplit, urljoin
except ImportError:
    from urllib.parse import urlsplit, urljoin

# Setup logging
log = logging.getLogger('sciluigi-interface')


class ContainerInfo():
    """
    A data object to store parameters related to running a specific
    tasks in a container (docker / batch / etc). Mostly around resources.
    """
    # Which container system to use
    # Docker by default. Extensible in the future for batch, slurm-singularity, etc
    engine = None
    # num vcpu required
    vcpu = None
    # max memory (mb)
    mem = None
    # Env
    env = None
    # Timeout in minutes
    timeout = None
    # Format is {'source_path': {'bind': '/container/path', 'mode': mode}}
    mounts = None
    # Local Container cache location. For things like singularity that need to pull
    # And create a local container
    container_cache = None

    # AWS specific stuff
    aws_jobRoleArn = None
    aws_s3_scratch_loc = None
    aws_batch_job_queue = None

    def __init__(self,
                 engine='docker',
                 vcpu=1,
                 mem=4096,
                 timeout=10080,  # Seven days of minutes
                 mounts={},
                 container_cache='.',
                 aws_jobRoleArn='',
                 aws_s3_scratch_loc='',
                 aws_batch_job_queue=''
                 ):
        self.engine = engine
        self.vcpu = vcpu
        self.mem = mem
        self.timeout = timeout
        self.mounts = mounts
        self.container_cache = container_cache
        self.aws_jobRoleArn = aws_jobRoleArn
        self.aws_s3_scratch_loc = aws_s3_scratch_loc
        self.aws_batch_job_queue = aws_batch_job_queue

    def __str__(self):
        """
        Return string of this information
        """
        return(
            "{} with Cpu {}, Mem {} MB, timeout {} secs, and container cache {}".format(
                self.engine,
                self.vcpu,
                self.mem,
                self.timeout,
                self.container_cache
            ))


class ContainerInfoParameter(sciluigi.parameter.Parameter):
    '''
    A specialized luigi parameter, taking ContainerInfo objects.
    '''

    def parse(self, x):
        if isinstance(x, ContainerInfo):
            return x
        else:
            log.error('parameter is not instance of ContainerInfo. It is instead {}'
                      .format(type(x)))
            raise Exception('parameter is not instance of ContainerInfo. It is instead {}'
                            .format(type(x)))


class ContainerHelpers():
    """
    Mixin with various methods and variables for running commands in containers using (Sci)-Luigi
    """
    # Other class-fields
    # Resource guidance for this container at runtime.
    containerinfo = ContainerInfoParameter(default=None)

    # The ID of the container (docker registry style).
    container = None

    def map_paths_to_container(self, paths, container_base_path='/mnt'):
        """
        Accepts a dictionary where the keys are identifiers for various targets
        and the value is the HOST path for that target

        What this does is find a common HOST prefix
        and remaps to the CONTAINER BASE PATH

        Returns a dict of the paths for the targets as they would be seen
        if the common prefix is mounted within the container at the container_base_path
        """
        common_prefix = os.path.commonprefix(
            [os.path.dirname(p) for p in paths.values()]
        )
        container_paths = {
            i: os.path.join(
                container_base_path,
                os.path.relpath(paths[i], common_prefix))
            for i in paths
        }
        return os.path.abspath(common_prefix), container_paths

    def make_fs_name(self, uri):
        uri_list = uri.split('://')
        if len(uri_list) == 1:
            name = uri_list[0]
        else:
            name = uri_list[1]
        keepcharacters = ('.', '_')
        return "".join(c if (c.isalnum() or c in keepcharacters) else '_' for c in name).rstrip()

    def ex(
            self,
            command,
            input_paths={},
            output_paths={},
            extra_params={},
            inputs_mode='ro',
            outputs_mode='rw'):
        if self.containerinfo.engine == 'docker':
            return self.ex_docker(
                command,
                input_paths,
                output_paths,
                extra_params,
                inputs_mode,
                outputs_mode
            )
        elif self.containerinfo.engine == 'aws_batch':
            return self.ex_aws_batch(
                command,
                input_paths,
                output_paths,
                extra_params,
                inputs_mode,
                outputs_mode
            )
        elif self.containerinfo.engine == 'singularity_slurm':
            return self.ex_singularity_slurm(
                command,
                input_paths,
                output_paths,
                extra_params,
                inputs_mode,
                outputs_mode
            )
        else:
            raise Exception("Container engine {} is invalid".format(self.containerinfo.engine))

    def ex_singularity_slurm(
            self,
            command,
            input_paths={},
            output_paths={},
            extra_params={},
            inputs_mode='ro',
            outputs_mode='rw'):
        """
        Run command in the container using singularity, with mountpoints
        command is assumed to be in python template substitution format
        """
        container_paths = {}
        mounts = self.containerinfo.mounts.copy()

        if len(output_paths) > 0:
            output_host_path_ca, output_container_paths = self.map_paths_to_container(
                output_paths,
                container_base_path='/mnt/outputs'
            )
            container_paths.update(output_container_paths)
            mounts[output_host_path_ca] = {'bind': '/mnt/outputs', 'mode': outputs_mode}

        if len(input_paths) > 0:
            input_host_path_ca, input_container_paths = self.map_paths_to_container(
                input_paths,
                container_base_path='/mnt/inputs'
            )
            # Handle the edge case where the common directory for inputs is equal to the outputs
            if len(output_paths) > 0 and (output_host_path_ca == input_host_path_ca):
                log.warn("Input and Output host paths the same {}".format(output_host_path_ca))
                # Repeat our mapping, now using the outputs path for both
                input_host_path_ca, input_container_paths = self.map_paths_to_container(
                    input_paths,
                    container_base_path='/mnt/outputs'
                )
            else:  # output and input paths different OR there are only input paths
                mounts[input_host_path_ca] = {'bind': '/mnt/inputs', 'mode': inputs_mode}

            # No matter what, add our mappings
            container_paths.update(input_container_paths)

        img_location = os.path.join(
            self.containerinfo.container_cache,
            "{}.singularity.img".format(self.make_fs_name(self.container))
            )
        log.info("Looking for singularity image {}".format(img_location))
        if not os.path.exists(img_location):
            log.info("No image at {} Creating....".format(img_location))
            try:
                os.makedirs(os.path.dirname(img_location))
            except FileExistsError:
                # No big deal
                pass
            # Singularity is dumb and can only pull images to the working dir
            # So, get our current working dir. 
            cwd = os.getcwd()
            # Move to our target dir
            os.chdir(os.path.dirname(img_location))
            # Attempt to pull our image
            pull_proc = subprocess.run(
                [
                    'singularity',
                    'pull',
                    '--name',
                    os.path.basename(img_location),
                    self.container
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            print(pull_proc)
            # Move back
            os.chdir(cwd)

        command = Template(command).substitute(container_paths)
        log.info("Attempting to run {} in {}".format(
                command,
                self.container
            ))

        command_list = [
            'singularity', 'exec'
        ]
        for mp in mounts:
            command_list += ['-B', "{}:{}:{}".format(mp, mounts[mp]['bind'], mounts[mp]['mode'])]
        command_list.append(img_location)
        command_list += shlex.split(command)
        command_proc = subprocess.run(
            command_list,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        log.info(command_proc.stdout)
        if command_proc.stderr:
            log.warn(command_proc.stderr)

    def ex_aws_batch(
            self,
            command,
            input_paths={},
            output_paths={},
            extra_params={},
            inputs_mode='ro',
            outputs_mode='rw'):
        """
        Run a command in a container using AWS batch.
        Handles uploading of files to / from s3 and then into the container. 
        Assumes the container has batch_command_wrapper.py
        """
        #
        # The steps:
        #   1) Upload local input files to S3 scratch bucket/key
        #   2) Register / retrieve the job definition
        #   3) submit the job definition with parameters filled with this specific command
        #   4) Retrieve the output paths from the s3 scratch bucket / key
        #

        # Only import AWS libs as needed
        import boto3
        batch_client = boto3.client('batch')
        s3_client = boto3.client('s3')

        run_uuid = str(uuid.uuid4())

        # 1. First a bit of file mapping / uploading of input items
        # We need mappings for both two and from S3 and from S3 to within the container
        # <local fs> <-> <s3> <-> <Container Mounts>
        # The script in the container, bucket_command_wrapper.py, handles the second half
        # practically, but we need to provide the link s3://bucket/key::/container/path/file::mode
        # the first half we have to do here.
        # s3_input_paths will hold the s3 path 
        container_paths = {}

        in_container_paths_from_s3 = {}
        in_container_paths_from_local_fs = {}
        s3_input_paths = {}
        need_s3_uploads = set()
        for (key, path) in input_paths.items():
            # First split the path, to see which scheme it is
            path_split = urlsplit(path)
            if path_split.scheme == 's3':
                # Nothing to do. Already an S3 path.
                in_container_paths_from_s3[key] = os.path.join(
                    path_split.netloc, 
                    path_split.path
                    )
                s3_input_paths[key] = path
            elif path_split.scheme == 'file' or path_split.scheme == '':
                # File path. Will need to upload to S3 to a temporary key within a bucket
                in_container_paths_from_local_fs[key] = path_split.path
                need_s3_uploads.add((key, path_split))
            else:
                raise ValueError("File storage scheme {} is not supported".format(
                    path_split.scheme
                ))

        in_from_local_fs_common_prefix = os.path.dirname(
            os.path.commonprefix([
                p for p in in_container_paths_from_local_fs.values()
            ])
        )

        for k, ps in need_s3_uploads:
            s3_file_temp_path = "{}{}/in/{}".format(
                self.containerinfo.aws_s3_scratch_loc,
                run_uuid,
                os.path.relpath(ps.path, in_from_local_fs_common_prefix)
            )
            s3_input_paths[k] = s3_file_temp_path
            log.info("Uploading {} to {}".format(
                input_paths[k],
                s3_input_paths[k],
            ))
            s3_client.upload_file(
                Filename=input_paths[k],
                Bucket=urlsplit(s3_input_paths[k]).netloc,
                Key=urlsplit(s3_input_paths[k]).path.strip('/'),
                ExtraArgs={
                    'ServerSideEncryption': 'AES256'
                }
            )
        # build our container paths for inputs from fs and S3
        for k in in_container_paths_from_local_fs:
            container_paths[k] = os.path.join(
                '/mnt/inputs/fs/',
                os.path.relpath(
                    in_container_paths_from_local_fs[k],
                    in_from_local_fs_common_prefix)
            )

        in_from_s3_common_prefix = os.path.dirname(
            os.path.commonprefix([
                p for p in in_container_paths_from_s3.values()
            ])
        )
        for k in in_container_paths_from_s3:
            container_paths[k] = os.path.join(
                '/mnt/inputs/s3/',
                os.path.relpath(
                    in_container_paths_from_s3[k],
                    in_from_s3_common_prefix)
                )

        # Outputs
        s3_output_paths = {}
        need_s3_downloads = set()
        out_container_paths_from_s3 = {}
        out_container_paths_from_local_fs = {}

        for (key, path) in output_paths.items():
            # First split the path, to see which scheme it is
            path_split = urlsplit(path)
            if path_split.scheme == 's3':
                # Nothing to do. Already an S3 path.
                s3_output_paths[key] = path
                out_container_paths_from_s3[key] = os.path.join(
                    path_split.netloc,
                    path_split.path
                    )
            elif path_split.scheme == 'file' or path_split.scheme == '':
                # File path. Will need to upload to S3 to a temporary key within a bucket
                need_s3_downloads.add((key, path_split))
                out_container_paths_from_local_fs[key] = path_split.path
            else:
                raise ValueError("File storage scheme {} is not supported".format(
                    path_split.scheme
                ))
        output_common_prefix = os.path.commonpath([
            os.path.dirname(os.path.abspath(ps[1].path))
            for ps in need_s3_downloads
        ])

        for k, ps in need_s3_downloads:
            s3_file_temp_path = "{}{}/out/{}".format(
                self.containerinfo.aws_s3_scratch_loc,
                run_uuid,
                os.path.relpath(ps.path, output_common_prefix)
            )
            s3_output_paths[k] = s3_file_temp_path

        # Make our container paths for outputs
        out_from_local_fs_common_prefix = os.path.dirname(
            os.path.commonprefix([
                p for p in out_container_paths_from_local_fs.values()
            ])
        )
        for k in out_container_paths_from_local_fs:
            container_paths[k] = os.path.join(
                '/mnt/outputs/fs/',
                os.path.relpath(
                    out_container_paths_from_local_fs[k], 
                    out_from_local_fs_common_prefix)
            )

        out_from_s3_common_prefix = os.path.dirname(
            os.path.commonprefix([
                p for p in out_container_paths_from_s3.values()
            ])
        )
        for k in out_container_paths_from_s3:
            container_paths[k] = os.path.join(
                '/mnt/outputs/s3/',
                os.path.relpath(
                    out_container_paths_from_s3[k],
                    out_from_s3_common_prefix)
                )

        # 2) Register / retrieve job definition for this container, command, and job role arn

        # Make a UUID based on the container / command
        job_def_name = "sl_containertask__{}".format(
                uuid.uuid5(
                    uuid.NAMESPACE_URL, 
                    self.container+self.containerinfo.aws_jobRoleArn+str(self.containerinfo.mounts)
                    )
            )

        # Search to see if this job is ALREADY defined.
        job_def_search = batch_client.describe_job_definitions(
            maxResults=1,
            status='ACTIVE',
            jobDefinitionName=job_def_name,
        )
        if len(job_def_search['jobDefinitions']) == 0:
            # Not registered yet. Register it now
            log.info(
                """Registering job definition for {} with role {} and mounts {} under name {}
                """.format(
                           self.container,
                           self.containerinfo.aws_jobRoleArn,
                           self.containerinfo.mounts,
                           job_def_name,
                ))
            # To be passed along for container properties
            aws_volumes = []
            aws_mountPoints = []
            for (host_path, container_details) in self.containerinfo.mounts.items():
                name = str(uuid.uuid5(uuid.NAMESPACE_URL, host_path))
                aws_volumes.append({
                    'host': {'sourcePath': host_path},
                    'name': name
                })
                if container_details['mode'].lower() == 'ro':
                    read_only = True
                else:
                    read_only = False
                aws_mountPoints.append({
                    'containerPath': container_details['bind'],
                    'sourceVolume': name,
                    'readOnly': read_only,
                })

            batch_client.register_job_definition(
                jobDefinitionName=job_def_name,
                type='container',
                containerProperties={
                    'image': self.container,
                    'vcpus': 1,
                    'memory': 1024,
                    'command': shlex.split(command),
                    'jobRoleArn': self.containerinfo.aws_jobRoleArn,
                    'mountPoints': aws_mountPoints,
                    'volumes': aws_volumes
                },
                timeout={
                    'attemptDurationSeconds': self.containerinfo.timeout * 60
                }
            )
        else:  # Already registered
            aws_job_def = job_def_search['jobDefinitions'][0]
            log.info('Found job definition for {} with job role {} under name {}'.format(
                aws_job_def['containerProperties']['image'],
                aws_job_def['containerProperties']['jobRoleArn'],
                job_def_name,
            ))

        # Build our container command list
        template_dict = container_paths.copy()
        template_dict.update(extra_params)
        container_command_list = [
            'bucket_command_wrapper',
            '--command', Template(command).safe_substitute(template_dict)
        ]
        # Add in our inputs
        for k in s3_input_paths:
            container_command_list += [
                '-DF',
                "{}::{}::{}".format(
                    s3_input_paths[k],
                    container_paths[k],
                    inputs_mode.lower()
                )
            ]

        # And our outputs
        for k in s3_output_paths:
            container_command_list += [
                '-UF',
                "{}::{}".format(
                    container_paths[k],
                    s3_output_paths[k]
                )
            ]

        # Submit the job
        job_submission = batch_client.submit_job(
            jobName=run_uuid,
            jobQueue=self.containerinfo.aws_batch_job_queue,
            jobDefinition=job_def_name,
            containerOverrides={
                'vcpus': self.containerinfo.vcpu,
                'memory': self.containerinfo.mem,
                'command': container_command_list,
            },
        )
        job_submission_id = job_submission.get('jobId')
        log.info("Running {} under jobId {}".format(
            container_command_list,
            job_submission_id
        ))
        while True:
            job_status = batch_client.describe_jobs(
                jobs=[job_submission_id]
            ).get('jobs')[0]
            if job_status.get('status') == 'SUCCEEDED' or job_status.get('status') == 'FAILED':
                break
            time.sleep(10)
        if job_status.get('status') != 'SUCCEEDED':
            raise Exception("Batch job failed. {}".format(
                job_status.get('statusReason')
            ))
        # Implicit else we succeeded
        # Now we need to copy back from S3 to our local filesystem
        for k, ps in need_s3_downloads:
            s3_client.download_file(
                Filename=ps.path,
                Bucket=urlsplit(s3_output_paths[k]).netloc,
                Key=urlsplit(s3_output_paths[k]).path.strip('/')
            )
        # And the inputs if we are rw
        if inputs_mode == 'rw':
            for k, ps in need_s3_uploads:
                s3_client.download_file(
                    Filename=ps.path,
                    Bucket=urlsplit(s3_input_paths[k]).netloc,
                    Key=urlsplit(s3_input_paths[k]).path.strip('/')
                )

        # Cleanup the temp S3
        for k, ps in need_s3_uploads:
            s3_client.delete_object(
                Bucket=urlsplit(s3_input_paths[k]).netloc,
                Key=urlsplit(s3_input_paths[k]).path.strip('/'),
            )

        # And done

    def ex_docker(
            self,
            command,
            input_paths={},
            output_paths={},
            extra_params={},
            inputs_mode='ro',
            outputs_mode='rw'):
        """
        Run command in the container using docker, with mountpoints
        command is assumed to be in python template substitution format
        """
        client = docker.from_env()
        container_paths = {}
        mounts = self.containerinfo.mounts.copy()

        if len(output_paths) > 0:
            output_host_path_ca, output_container_paths = self.map_paths_to_container(
                output_paths,
                container_base_path='/mnt/outputs'
            )
            container_paths.update(output_container_paths)
            mounts[output_host_path_ca] = {'bind': '/mnt/outputs', 'mode': outputs_mode}

        if len(input_paths) > 0:
            input_host_path_ca, input_container_paths = self.map_paths_to_container(
                input_paths,
                container_base_path='/mnt/inputs'
            )
            # Handle the edge case where the common directory for inputs is equal to the outputs
            if len(output_paths) > 0 and (output_host_path_ca == input_host_path_ca):
                log.warn("Input and Output host paths the same {}".format(output_host_path_ca))
                # Repeat our mapping, now using the outputs path for both
                input_host_path_ca, input_container_paths = self.map_paths_to_container(
                    input_paths,
                    container_base_path='/mnt/outputs'
                )
            else:  # output and input paths different OR there are only input paths
                mounts[input_host_path_ca] = {'bind': '/mnt/inputs', 'mode': inputs_mode}

            # No matter what, add our mappings
            container_paths.update(input_container_paths)

        template_dict = container_paths.copy()
        template_dict.update(extra_params)
        command = Template(command).substitute(template_dict)

        try:
            log.info("Attempting to run {} in {}".format(
                command,
                self.container
            ))
            stdout = client.containers.run(
                image=self.container,
                command=['bash', '-c', command],
                volumes=mounts,
                mem_limit="{}m".format(self.containerinfo.mem),
            )
            log.info(stdout)
            return (0, stdout, "")
        except docker.errors.ContainerError as e:
            log.error("Non-zero return code from the container: {}".format(e))
            return (-1, "", "")
        except docker.errors.ImageNotFound:
            log.error("Could not find container {}".format(
                self.container)
                )
            return (-1, "", "")
        except docker.errors.APIError as e:
            log.error("Docker Server failed {}".format(e))
            return (-1, "", "")
        except Exception as e:
            log.error("Unknown error occurred: {}".format(e))
            return (-1, "", "")


# ================================================================================

class ContainerTask(ContainerHelpers, sciluigi.task.Task):
    '''
    luigi task that includes the ContainerHelpers mixin.
    '''
    pass
