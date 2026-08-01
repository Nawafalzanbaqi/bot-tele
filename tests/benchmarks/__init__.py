"""Measurements with a ceiling attached.

A benchmark that only prints a number is a number nobody reads. Every test here
measures something the Raspberry Pi deployment actually cares about and then
asserts a bound on it, so a change that makes the system twice as hungry fails
the build instead of being discovered on the device six months later.

The bounds are deliberately generous - several times the observed cost on
ordinary hardware. They are **regression detectors, not performance targets**:
the value of the assertion is that it catches an accidental order of magnitude,
and a tight bound on a shared CI machine only teaches people to skip the suite.
Absolute figures for reference hardware live in
``docs/operations/performance-report.md``.

Nothing here needs a network, a database or a real download.
"""
