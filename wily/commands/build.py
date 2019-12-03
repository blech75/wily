"""
Builds a cache based on a source-control history.

TODO : Convert .gitignore to radon ignore patterns to make the build more efficient.

"""
import os
import pathlib
import multiprocessing
from progress.bar import Bar

from wily import logger
from wily.state import State

from wily.archivers.git import InvalidGitRepositoryError
from wily.archivers import FilesystemArchiver

from wily.operators import resolve_operator


def run_operator(operator, revision, config, seed):
    """Run an operator for the multiprocessing pool. Not called directly."""
    if seed:
        targets = config.targets
    else:  # Only target changed files
        targets = [str(pathlib.Path(config.path) / pathlib.Path(file)) for file in revision.files]

    instance = operator.cls(config, targets)
    logger.debug(f"Running {operator.name} operator on {revision.key}, seed={seed}")

    data = instance.run(revision, config)

    # Normalise paths for non-seed passes
    for key in list(data.keys()):
        if os.path.isabs(key):
            rel = os.path.relpath(key, config.path)
            data[rel] = data[key]
            del data[key]

    return operator.name, data


def build(config, archiver, operators):
    """
    Build the history given a archiver and collection of operators.

    :param config: The wily configuration
    :type  config: :namedtuple:`wily.config.WilyConfig`

    :param archiver: The archiver to use
    :type  archiver: :namedtuple:`wily.archivers.Archiver`

    :param operators: The list of operators to execute
    :type operators: `list` of :namedtuple:`wily.operators.Operator`
    """
    try:
        logger.debug(f"Using {archiver.name} archiver module")
        archiver = archiver.cls(config)
        revisions = archiver.revisions(config.path, config.max_revisions)
    except InvalidGitRepositoryError:
        # TODO: This logic shouldn't really be here (SoC)
        logger.info(f"Defaulting back to the filesystem archiver, not a valid git repo")
        archiver = FilesystemArchiver(config)
        revisions = archiver.revisions(config.path, config.max_revisions)
    except Exception as e:
        if hasattr(e, "message"):
            logger.error(f"Failed to setup archiver: '{e.message}'")
        else:
            logger.error(f"Failed to setup archiver: '{type(e)} - {e}'")
        exit(1)

    state = State(config, archiver=archiver)
    # Check for existence of cache, else provision
    state.ensure_exists()

    index = state.index[archiver.name]

    # remove existing revisions from the list
    revisions = [revision for revision in revisions if revision not in index][::-1]

    logger.info(
        f"Found {len(revisions)} revisions from '{archiver.name}' archiver in '{config.path}'."
    )

    _op_desc = ",".join([operator.name for operator in operators])
    logger.info(f"Running operators - {_op_desc}")

    bar = Bar("Processing", max=len(revisions) * len(operators))
    state.operators = operators

    # Index all files the first time, only scan changes afterward
    seed = True
    prev_roots = None
    try:
        with multiprocessing.Pool(processes=len(operators)) as pool:
            for revision in revisions:
                # Checkout target revision
                archiver.checkout(revision, config.checkout_options)
                stats = {"operator_data": {}}

                # Run each operator as a separate process
                data = pool.starmap(
                    run_operator,
                    [(operator, revision, config, seed) for operator in operators],
                )

                # Map the data back into a dictionary
                for operator_name, result in data:
                    # find all unique directories in the results
                    roots = {pathlib.Path(entry).parents[0] for entry in result.keys()}
                    indices = set(result.keys())

                    # For a seed run, there is no previous change set, so use current
                    if seed:
                        prev_roots = roots
                        prev_indices = indices
                    roots = prev_roots | roots
                    
                    # Copy the ir from any unchanged files from the prev revision
                    if not seed:
                        missing_indices = prev_indices - indices
                        # TODO: Check existence of file path.
                        logger.debug(f"Mapping back metrics from {missing_indices}")
                        for missing in missing_indices:
                            if missing not in roots:
                                result[missing] = prev_stats["operator_data"][operator_name][missing]

                    # Aggregate metrics across all root paths using the aggregate function in the metric
                    for root in roots:
                        # find all matching entries recursively
                        aggregates = [
                            path
                            for path in result.keys()
                            if root in pathlib.Path(path).parents
                        ]
                        result[str(root)] = {"total": {}}
                        # aggregate values
                        for metric in resolve_operator(operator_name).cls.metrics:
                            func = metric.aggregate
                            values = [
                                result[aggregate]["total"][metric.name]
                                for aggregate in aggregates
                                if aggregate in result
                                and metric.name in result[aggregate]["total"]
                            ]
                            if len(values) > 0:
                                result[str(root)]["total"][metric.name] = func(values)

                    prev_indices = set(result.keys())
                    prev_roots = roots
                    stats["operator_data"][operator_name] = result
                    bar.next()

                prev_stats = stats
                seed = False
                ir = index.add(revision, operators=operators)
                ir.store(config, archiver, stats)
        index.save()
        bar.finish()
    except Exception as e:
        logger.error(f"Failed to build cache: {type(e)}: '{e}'")
        raise e
    finally:
        # Reset the archive after every run back to the head of the branch
        archiver.finish()
