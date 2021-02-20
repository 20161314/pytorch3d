# Copyright (c) Facebook, Inc. and its affiliates. All rights reserved.
import json
import os
import unittest
from collections import Counter
from pathlib import Path


# This file groups together tests which look at the code without running it.
# When running the tests inside conda's build, the code is not available.
in_conda_build = os.environ.get("CONDA_BUILD_STATE", "") == "TEST"


class TestBuild(unittest.TestCase):
    @unittest.skipIf(in_conda_build, "In conda build")
    def test_name_clash(self):
        # For setup.py, all translation units need distinct names, so we
        # cannot have foo.cu and foo.cpp, even in different directories.
        test_dir = Path(__file__).resolve().parent
        source_dir = test_dir.parent / "pytorch3d"

        stems = []
        for extension in [".cu", ".cpp"]:
            files = source_dir.glob(f"**/*{extension}")
            stems.extend(f.stem for f in files)

        counter = Counter(stems)
        for k, v in counter.items():
            self.assertEqual(v, 1, f"Too many files with stem {k}.")

    @unittest.skipIf(in_conda_build, "In conda build")
    def test_copyright(self):
        test_dir = Path(__file__).resolve().parent
        root_dir = test_dir.parent

        extensions = ("py", "cu", "cuh", "cpp", "h", "hpp", "sh")

        expect = (
            "Copyright (c) Facebook, Inc. and its affiliates."
            + " All rights reserved.\n"
        )

        files_missing_copyright_header = []

        for extension in extensions:
            for path in root_dir.glob(f"**/*.{extension}"):
                if str(path).endswith(
                    "pytorch3d/transforms/external/kornia_angle_axis_to_rotation_matrix.py"
                ):
                    continue
                if str(path).endswith("pytorch3d/csrc/pulsar/include/fastermath.h"):
                    continue
                with open(path) as f:
                    firstline = f.readline()
                    if firstline.startswith(("# -*-", "#!")):
                        firstline = f.readline()
                    if not firstline.endswith(expect):
                        files_missing_copyright_header.append(str(path))

        if len(files_missing_copyright_header) != 0:
            self.fail("\n".join(files_missing_copyright_header))

    @unittest.skipIf(in_conda_build, "In conda build")
    def test_valid_ipynbs(self):
        # Check that the ipython notebooks are valid json
        test_dir = Path(__file__).resolve().parent
        tutorials_dir = test_dir.parent / "docs" / "tutorials"
        tutorials = sorted(tutorials_dir.glob("*.ipynb"))

        for tutorial in tutorials:
            with open(tutorial) as f:
                json.load(f)
