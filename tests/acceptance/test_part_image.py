#!/usr/bin/python
# Copyright 2017 Northern.tech AS
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

from fabric.api import *

import pytest
import subprocess
import os
import re
import tempfile

# Make sure common is imported after fabric, because we override some functions.
from common import *

def align_up(bytes, alignment):
    """Rounds bytes up to nearest alignment."""
    return (int(bytes) + int(alignment) - 1) / int(alignment) * int(alignment)


def extract_partition(img, number):
    output = subprocess.Popen(["fdisk", "-l", "-o", "device,start,end", img],
                              stdout=subprocess.PIPE)
    for line in output.stdout:
        if re.search("img%d" % number, line) is None:
            continue

        match = re.match("\s*\S+\s+(\S+)\s+(\S+)", line)
        assert(match is not None)
        start = int(match.group(1))
        end = (int(match.group(2)) + 1)
    output.wait()

    subprocess.check_call(["dd", "if=" + img, "of=img%d.fs" % number,
                           "skip=%d" % start, "count=%d" % (end - start)])


@pytest.mark.only_with_image('sdimg', 'uefiimg', 'biosimg')
@pytest.mark.min_mender_version("1.0.0")
class TestPartitionImage:

    @staticmethod
    def verify_fstab(data):
        lines = data.split('\n')

        occurred = {}

        # No entry should occur twice.
        for line in lines:
            cols = line.split()
            if len(line) == 0 or line[0] == '#' or len(cols) < 2:
                continue
            assert occurred.get(cols[1]) is None, "%s appeared twice in fstab:\n%s" % (cols[1], data)
            occurred[cols[1]] = True

    def test_total_size(self, bitbake_variables, latest_part_image):
        """Test that the total size of the img is correct."""

        total_size_actual = os.stat(latest_part_image).st_size
        total_size_max_expected = int(bitbake_variables['MENDER_STORAGE_TOTAL_SIZE_MB']) * 1024 * 1024
        total_overhead = int(bitbake_variables['MENDER_PARTITIONING_OVERHEAD_KB']) * 1024

        assert(total_size_actual <= total_size_max_expected)
        assert(total_size_actual >= total_size_max_expected - total_overhead)

    def test_partition_alignment(self, bitbake_path, bitbake_variables, latest_part_image):
        """Test that partitions inside the img are aligned correctly, and
        correct sizes."""

        fdisk = subprocess.Popen(["fdisk", "-l", "-o", "start,end", latest_part_image], stdout=subprocess.PIPE)
        payload = False
        parts_start = []
        parts_end = []
        for line in fdisk.stdout:
            line = line.strip()
            if payload:
                match = re.match("^\s*([0-9]+)\s+([0-9]+)\s*$", line)
                assert(match is not None)
                parts_start.append(int(match.group(1)) * 512)
                # +1 because end position is inclusive.
                parts_end.append((int(match.group(2)) + 1) * 512)
            elif re.match(".*start.*end.*", line, re.IGNORECASE) is not None:
                # fdisk precedes the output with lots of uninteresting stuff,
                # this gets us to the meat (/me wishes for a "machine output"
                # mode).
                payload = True

        fdisk.wait()

        alignment = int(bitbake_variables['MENDER_PARTITION_ALIGNMENT'])
        total_size = int(bitbake_variables['MENDER_STORAGE_TOTAL_SIZE_MB']) * 1024 * 1024
        part_overhead = int(bitbake_variables['MENDER_PARTITIONING_OVERHEAD_KB']) * 1024
        boot_part_size = int(bitbake_variables['MENDER_BOOT_PART_SIZE_MB']) * 1024 * 1024
        data_part_size = int(bitbake_variables['MENDER_DATA_PART_SIZE_MB']) * 1024 * 1024

        if "mender-uboot" in bitbake_variables['DISTRO_FEATURES']:
            uboot_env_size = os.stat(os.path.join(bitbake_variables["DEPLOY_DIR_IMAGE"], "uboot.env")).st_size
            # Uboot environment should be aligned.
            assert(uboot_env_size % alignment == 0)
        else:
            uboot_env_size = 0

        # First partition should start after exactly one alignment, plus the
        # U-Boot environment.
        assert(parts_start[0] == alignment + uboot_env_size)

        # Subsequent partitions should start where previous one left off.
        assert(parts_start[1] == parts_end[0])
        assert(parts_start[2] == parts_end[1])
        assert(parts_start[3] == parts_end[2])

        # Partitions should extend for their size rounded up to alignment.
        # No set size for Rootfs partitions, so cannot check them.
        # Boot partition.
        assert(parts_end[0] == parts_start[0] + align_up(boot_part_size, alignment))
        # Data partition.
        assert(parts_end[3] == parts_start[3] + align_up(data_part_size, alignment))

        # End of the last partition can be smaller than total image size, but
        # not by more than the calculated overhead..
        assert(parts_end[3] <= total_size)
        assert(parts_end[3] >= total_size - part_overhead)


    def test_device_type(self, latest_part_image, bitbake_variables, bitbake_path):
        """Test that device type file is correctly embedded."""

        try:
            extract_partition(latest_part_image, 4)

            subprocess.check_call(["debugfs", "-R", "dump -p /mender/device_type device_type", "img4.fs"])

            assert(os.stat("device_type").st_mode & 0777 == 0444)

            fd = open("device_type")

            lines = fd.readlines()
            assert(len(lines) == 1)
            lines[0] = lines[0].rstrip('\n\r')
            assert(lines[0] == "device_type=%s" % bitbake_variables["MENDER_DEVICE_TYPE"])

            fd.close()

        except:
            subprocess.call(["ls", "-l", "device_type"])
            print("Contents of artifact_info:")
            subprocess.call(["cat", "device_type"])
            raise

        finally:
            try:
                os.remove("img4.fs")
                os.remove("device_type")
            except:
                pass

    def test_data_ownership(self, latest_part_image, bitbake_variables, bitbake_path):
        """Test that the owner of files on the data partition is root."""

        try:
            extract_partition(latest_part_image, 4)

            def check_dir(dir):
                ls = subprocess.Popen(["debugfs", "-R" "ls -l -p %s" % dir, "img4.fs"], stdout=subprocess.PIPE)
                entries = ls.stdout.readlines()
                ls.wait()

                for entry in entries:
                    entry = entry.strip()

                    if len(entry) == 0:
                        # debugfs might output empty lines too.
                        continue

                    columns = entry.split('/')

                    if columns[1] == "0":
                        # Inode 0 is some weird file inside lost+found, skip it.
                        continue

                    assert(columns[3] == "0")
                    assert(columns[4] == "0")

                    mode = int(columns[2], 8)
                    # Recurse into directories.
                    if mode & 040000 != 0 and columns[5] != "." and columns[5] != "..":
                        check_dir(os.path.join(dir, columns[5]))

            check_dir("/")

        finally:
            try:
                os.remove("img4.fs")
            except:
                pass

    def test_fstab_correct(self, bitbake_path, latest_part_image):
        with make_tempdir() as tmpdir:
            old_cwd_fd = os.open(".", os.O_RDONLY)
            os.chdir(tmpdir)
            try:
                extract_partition(latest_part_image, 2)
                subprocess.check_call(["debugfs", "-R", "dump -p /etc/fstab fstab", "img2.fs"])
                with open("fstab") as fd:
                    data = fd.read()
                TestPartitionImage.verify_fstab(data)
            finally:
                os.fchdir(old_cwd_fd)
                os.close(old_cwd_fd)

    @pytest.mark.only_with_distro_feature('mender-grub')
    def test_mender_grubenv(self, bitbake_path, latest_part_image, bitbake_variables):
        if "mender-bios" in bitbake_variables['DISTRO_FEATURES'].split():
            env_dir = "/"
        else:
            env_dir = "/EFI/BOOT/"

        with make_tempdir() as tmpdir:
            old_cwd_fd = os.open(".", os.O_RDONLY)
            os.chdir(tmpdir)
            try:
                extract_partition(latest_part_image, 1)
                for env_name in ["mender_grubenv1", "mender_grubenv2"]:
                    subprocess.check_call(["mcopy", "-i", "img1.fs", "::%s%s/env" % (env_dir, env_name), "."])
                    with open("env") as fd:
                        data = fd.read()
                    os.unlink("env")
                    assert "mender_boot_part=%s" % bitbake_variables['MENDER_ROOTFS_PART_A'][-1] in data
                    assert "upgrade_available=0" in data
                    assert "bootcount=0" in data
            finally:
                os.fchdir(old_cwd_fd)
                os.close(old_cwd_fd)


    @pytest.mark.min_mender_version('1.0.0')
    def test_boot_partition_population(self, prepared_test_build, bitbake_path):
        # Notice in particular a mix of tabs, newlines and spaces. All there to
        # check that whitespace it treated correctly.
        add_to_local_conf(prepared_test_build, """
IMAGE_INSTALL_append = " test-boot-files"

IMAGE_BOOT_FILES_append = " deployed-test1 deployed-test-dir2/deployed-test2 \
	deployed-test3;renamed-deployed-test3 \
 deployed-test-dir4/deployed-test4;renamed-deployed-test4	deployed-test5;renamed-deployed-test-dir5/renamed-deployed-test5 \
deployed-test-dir6/deployed-test6;renamed-deployed-test-dir6/renamed-deployed-test6 \
deployed-test-dir7/* \
deployed-test-dir8/*;./ \
deployed-test-dir9/*;renamed-deployed-test-dir9/ \
"
""")
        run_bitbake(prepared_test_build)

        image = latest_build_artifact(prepared_test_build['build_dir'], "core-image*.*img")
        extract_partition(image, 1)
        try:
            listing = run_verbose("mdir -i img1.fs -b -/", capture=True).split()
            expected = [
                "::/deployed-test1",
                "::/deployed-test2",
                "::/renamed-deployed-test3",
                "::/renamed-deployed-test4",
                "::/renamed-deployed-test-dir5/renamed-deployed-test5",
                "::/renamed-deployed-test-dir6/renamed-deployed-test6",
                "::/deployed-test7",
                "::/deployed-test8",
                "::/renamed-deployed-test-dir9/deployed-test9",
            ]
            assert(all([item in listing for item in expected]))

            add_to_local_conf(prepared_test_build, 'IMAGE_BOOT_FILES_append = " conflict-test1"')
            try:
                run_bitbake(prepared_test_build)
                pytest.fail("Bitbake succeeded, but should have failed with a file conflict")
            except subprocess.CalledProcessError:
                pass
        finally:
            os.remove("img1.fs")
