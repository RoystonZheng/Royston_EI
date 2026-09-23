Vanilla_prompt = """
Given these obstacle coordinates in a grid:
{prompt}

Plan a continuous path from ({start_x}, {start_y}) to ({end_x}, {end_y}) while avoiding all obstacles.
Every move must change exactly one coordinate by 1; diagonal moves are forbidden.
Return only the path in the form [(x1, y1), (x2, y2), ...].
If no valid path exists, return [].
"""

independent_path_CoT = """
Here are the obstacle coordinates in the grid:
{prompt}

Plan a path from ({start_x}, {start_y}) to ({end_x}, {end_y}) while avoiding obstacles.
Use only cardinal moves of exactly one grid cell. Check that the start and end are valid,
then construct a valid path. Return only [(x1, y1), (x2, y2), ...] or [].
"""

Few_shot_learning_prompt = """
Plan a short cardinal-move path from ({start_x}, {start_y}) to ({end_x}, {end_y}).
The rectangular obstacles are:
{prompt}

Stay inside the grid and avoid every obstacle cell. Each step must change exactly one
coordinate by 1. Return only [(x1, y1), ..., (xn, yn)] or [].
"""

algorithm_integration_CoT = """
Plan a shortest cardinal-move path from ({start_x}, {start_y}) to ({end_x}, {end_y}).
The rectangular obstacles are:
{prompt}

Use Dijkstra's algorithm principles: initialize the start cost to 0, expand valid
four-neighbor cells, and reconstruct the shortest path. Return only the path list or [].
"""

Dijkstra_algorithm_prompt = """
Here are the obstacle coordinates in the grid:
{prompt}

Calculate the shortest path from ({start_x}, {start_y}) to ({end_x}, {end_y}) with
Dijkstra's algorithm. Moves are up, down, left, or right by exactly one cell.
Avoid obstacles and remain in bounds. Return only [(x1, y1), ..., (xn, yn)] or [].
"""
