import unittest

from rhylthyme.program_runner import ProgramRunner


class TestActorsFeature(unittest.TestCase):
    """Test the actors feature in the ProgramRunner."""

    def test_actors_default_constraints(self):
        """Test that tasks without explicit constraints use the actors count."""
        # Create a program with actors=3 and tasks with and without explicit constraints
        program = {
            "programId": "test-actors",
            "name": "Test Actors Feature",
            "actors": 3,
            "tracks": [
                {
                    "trackId": "track1",
                    "name": "Track 1",
                    "steps": [
                        {
                            "stepId": "step1",
                            "name": "Step 1",
                            "task": "explicit-task",
                            "startTrigger": {"type": "programStart"},
                            "duration": {"type": "fixed", "seconds": 60},
                        },
                        {
                            "stepId": "step2",
                            "name": "Step 2",
                            "task": "implicit-task",
                            "startTrigger": {"type": "programStart"},
                            "duration": {"type": "fixed", "seconds": 60},
                        },
                    ],
                }
            ],
            "resourceConstraints": [{"task": "explicit-task", "maxConcurrent": 5}],
        }

        # Initialize the program runner
        runner = ProgramRunner(program)

        # Check that explicit constraints are preserved
        self.assertEqual(runner.resource_constraints["explicit-task"], 5)

        # Check that implicit constraints use the actors count
        self.assertEqual(runner.resource_constraints["implicit-task"], 3)

    def test_no_actors_property(self):
        """Test behavior when no actors property is provided."""
        # Create a program without actors property
        program = {
            "programId": "test-no-actors",
            "name": "Test No Actors",
            "tracks": [
                {
                    "trackId": "track1",
                    "name": "Track 1",
                    "steps": [
                        {
                            "stepId": "step1",
                            "name": "Step 1",
                            "task": "explicit-task",
                            "startTrigger": {"type": "programStart"},
                            "duration": {"type": "fixed", "seconds": 60},
                        },
                        {
                            "stepId": "step2",
                            "name": "Step 2",
                            "task": "implicit-task",
                            "startTrigger": {"type": "programStart"},
                            "duration": {"type": "fixed", "seconds": 60},
                        },
                    ],
                }
            ],
            "resourceConstraints": [{"task": "explicit-task", "maxConcurrent": 5}],
        }

        # Initialize the program runner
        runner = ProgramRunner(program)

        # Check that explicit constraints are preserved
        self.assertEqual(runner.resource_constraints["explicit-task"], 5)

        # Check that implicit constraints use the default actors count (1)
        self.assertEqual(runner.resource_constraints["implicit-task"], 1)

    def test_actors_zero_or_negative(self):
        """Test that actors count is at least 1 even if set to 0 or negative."""
        # Create a program with actors=0
        program = {
            "programId": "test-zero-actors",
            "name": "Test Zero Actors",
            "actors": 0,  # Invalid value, should be treated as 1
            "tracks": [
                {
                    "trackId": "track1",
                    "name": "Track 1",
                    "steps": [
                        {
                            "stepId": "step1",
                            "name": "Step 1",
                            "task": "implicit-task",
                            "startTrigger": {"type": "programStart"},
                            "duration": {"type": "fixed", "seconds": 60},
                        }
                    ],
                }
            ],
            "resourceConstraints": [],
        }

        # Initialize the program runner
        runner = ProgramRunner(program)

        # Check that actors count is at least 1
        self.assertGreaterEqual(runner.actors, 1)

        # Check that implicit constraints use at least 1
        self.assertGreaterEqual(runner.resource_constraints["implicit-task"], 1)


if __name__ == "__main__":
    unittest.main()
