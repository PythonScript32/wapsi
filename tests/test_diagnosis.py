"""
Diagnosis must map raw gateway strings to the right category, deterministically.
"""

# TODO: each RAW_REASONS string maps to its expected category
# TODO: an unknown string routes to the LLM path, and the result is constrained
#       to the valid enum
# TODO: the LLM can never override an unambiguous rule match
