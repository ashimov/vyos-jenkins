import json
import logging
import os
from shlex import quote
import shutil

from lib.helpers import execute, quote_all, project_dir, ProcessException


class Docker:
    def __init__(self, image_name, branch, vyos_mount_dir):
        self.image_name = image_name
        self.branch = branch
        self.vyos_mount_dir = vyos_mount_dir

    def get_full_image_name(self):
        return "%s:%s" % (self.image_name, self.branch)

    def pull(self, passthrough=True):
        docker_image = self.get_full_image_name()
        previous_docker_image = "previous-%s" % docker_image

        # We mark current image with custom tag, so we don't lose track when image gets updated because then the
        # regular tag will shift to the new image from the old image.
        try:
            execute("docker tag %s %s" % quote_all(docker_image, previous_docker_image))
        except ProcessException:
            pass  # Ignore if image doesn't exist.

        execute("docker pull %s" % quote_all(docker_image), passthrough=passthrough)

        # Now we compare the ID of regular tag and previous tag and delete if they differ
        output = execute("docker images --format json").strip()
        current_id = None
        previous_id = None
        for line in output.split("\n"):  # Weird JSON format where each item is on newline with standalone JSON.
            line = line.strip()
            image = json.loads(line)
            if image["Repository"] == self.image_name and image["Tag"] == self.branch:
                current_id = image["ID"]
            elif image["Repository"] == "previous-%s" % self.image_name and image["Tag"] == self.branch:
                previous_id = image["ID"]

        # Finally delete the previous image if it's not the same image.
        # Or remove just the previous tag if the image wasn't updated.
        try:
            if previous_id is not None:
                execute("docker rmi %s" % quote_all(previous_docker_image))
                if current_id is not None and current_id != previous_id:
                    execute("docker rmi %s" % quote_all(previous_id))
        except ProcessException:
            pass  # Ignore if image doesn't exist.

    def rmtree(self, target):
        # This is sanity check, we really don't want to rm -rf something that isn't ours by mistake.
        target = os.path.realpath(target)
        if not target.startswith(project_dir):
            raise Exception("Delete of %s DENIED, target is outside project_dir (%s)" % (target, project_dir))

        try:
            shutil.rmtree(target)
        except PermissionError:
            # I know, this is privilege escalation, but there is no other way.
            # Unfortunately the docker container creates some files as root, and thus we don't have a choice.
            # What the container messes up, the container needs to clean up.
            # Here you can see the inherent security issue if container has root privileges.
            # Any regular user with docker access can leverage the container to do anything as root.
            # But this container needs to run as root in order to do its job so this is necessary evil.
            # Ideally the container should be made not to leave behind files owned by root, tell this to the VyOS team.
            logging.info("Deleting '%s' by force (privilege escalation)" % target)
            self.run("bash -c %s" % quote("sudo rm -rf /delete-me/*"), extra_mounts=[
                (target, "/delete-me")
            ])
            shutil.rmtree(target)

    def run(self, command, work_dir="/vyos", extra_mounts=None, passthrough=True, log_command=None):
        pieces: list = [
            "docker run --rm -t",
        ]

        if os.path.exists(self.vyos_mount_dir):
            pieces.append("-v %s:/vyos" % quote(self.vyos_mount_dir))

        if extra_mounts is not None:
            for mount in extra_mounts:
                pieces.append("-v %s:%s" % quote_all(*mount))

        pieces.extend([
            "-w %s --privileged --sysctl net.ipv6.conf.lo.disable_ipv6=0" % quote(work_dir),
            "-e GOSU_UID=%s -e GOSU_GID=%s" % (os.getuid(), os.getgid()),
            quote(self.get_full_image_name()),
        ])

        if log_command:
            placeholder = command if log_command is True else log_command
            visual_pieces = pieces.copy()
            visual_pieces.append(placeholder)
            logging.info("Using docker run command: '%s'" % " ".join(visual_pieces))

        pieces.append(command)

        docker_run_command = " ".join(pieces)
        return execute(docker_run_command, passthrough=passthrough, passthrough_prefix="DOCKER: ")
