# -*- coding: utf-8 -*-
#
# Copyright (C) 2014-2020 Bitergia
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#
# Authors:
#     Santiago Dueñas <sduenas@bitergia.com>
#     Miguel Ángel Fernández <mafesan@bitergia.com>
#

import itertools

import django_rq
import django_rq.utils
import pandas
import rq

from .api import enroll, merge
from .context import SortingHatContext
from .errors import BaseError, NotFoundError, EqualIndividualError
from .log import TransactionsLog
from .models import Individual
from .recommendations.engine import RecommendationEngine


MAX_CHUNK_SIZE = 2000


def find_job(job_id):
    """Find a job in the jobs registry.

    Search for a job using its identifier. When the job is
    not found, a `NotFoundError` exception is raised.

    :param job_id: job identifier

    :returns: a Job instance

    :raises NotFoundError: when the job identified by `job_id`
        is not found.
    """
    queue = django_rq.get_queue()
    jobs = django_rq.utils.get_jobs(queue, [job_id])

    if not jobs:
        raise NotFoundError(entity=job_id)

    return jobs[0]


@django_rq.job
def recommend_affiliations(ctx, uuids=None):
    """Generate a list of affiliation recommendations from a set of individuals.

    This function generates a list of recommendations which include the
    organizations where individuals can be affiliated.
    This job returns a dictionary with which individuals are recommended to be
    affiliated to which organization.

    Individuals are defined by any of their valid keys or UUIDs.
    When the parameter `uuids` is empty, the job will take all
    the individuals stored in the registry.

    :param ctx: context where this job is run
    :param uuids: list of individuals identifiers

    :returns: a dictionary with which individuals are recommended to be
        affiliated to which organization.
    """
    if not uuids:
        uuids = Individual.objects.values_list('mk', flat=True).iterator()
    else:
        uuids = iter(uuids)

    results = {}
    job_result = {
        'results': results
    }

    engine = RecommendationEngine()

    # Create a new context to include the reference
    # to the job id that will perform the transaction.
    job = rq.get_current_job()
    job_ctx = SortingHatContext(ctx.user, job.id)

    # Create an empty transaction to log which job
    # will generate the enroll transactions.
    trxl = TransactionsLog.open('recommend_affiliations', job_ctx)

    for chunk in _iter_split(uuids, size=MAX_CHUNK_SIZE):
        for rec in engine.recommend('affiliation', chunk):
            results[rec.key] = rec.options

    trxl.close()

    return job_result


@django_rq.job
def recommend_matches(ctx, source_uuids, target_uuids, criteria, verbose=False):
    """Generate a list of affiliation recommendations from a set of individuals.

    This function generates a list of recommendations which include the
    matching identities from the individuals which can be merged with.
    This job returns a dictionary with which individuals are recommended to be
    merged to which individual (or which identities is `verbose` mode is activated).

    Individuals both for `source_uuids` and `target_uuids` are defined by any of
    their valid keys or UUIDs. When the parameter `target_uuids` is empty, the
    recommendation engine will take all the individuals stored in the registry,
    so matches will be found comparing the identities from the individuals in
    `source_uuids` against all the identities on the registry.

    :param ctx: context where this job is run
    :param source_uuids: list of individuals identifiers to look matches for
    :param target_uuids: list of individuals identifiers where to look for matches
    :param criteria: list of fields which the match will be based on
        (`email`, `name` and/or `username`)
    :param verbose: if set to `True`, the match results will be composed by individual
        identities (even belonging to the same individual).

    :returns: a dictionary with which individuals are recommended to be
        merged to which individual or which identities.
    """

    results = {}
    job_result = {
        'results': results
    }

    engine = RecommendationEngine()

    # Create a new context to include the reference
    # to the job id that will perform the transaction.
    job = rq.get_current_job()
    job_ctx = SortingHatContext(ctx.user, job.id)

    trxl = TransactionsLog.open('recommend_matches', job_ctx)

    for rec in engine.recommend('matches', source_uuids, target_uuids, criteria, verbose):
        results[rec.key] = list(rec.options)

    trxl.close()

    return job_result


@django_rq.job
def affiliate(ctx, uuids=None):
    """Affiliate a set of individuals using recommendations.

    This function automates the affiliation process obtaining
    a list of recommendations where individuals can be
    affiliated. After that, individuals are enrolled to them.
    This job returns a dictionary with which individuals were
    enrolled and the errors generated during this process.

    Individuals are defined by any of their valid keys or UUIDs.
    When the parameter `uuids` is empty, the job will take all
    the individuals stored in the registry.

    :param ctx: context where this job is run
    :param uuids: list of individuals identifiers

    :returns: a dictionary with which individuals were enrolled
        and the errors found running the job
    """
    if not uuids:
        uuids = Individual.objects.values_list('mk', flat=True).iterator()
    else:
        uuids = iter(uuids)

    results = {}
    errors = []
    job_result = {
        'results': results,
        'errors': errors
    }

    engine = RecommendationEngine()

    # Create a new context to include the reference
    # to the job id that will perform the transaction.
    job = rq.get_current_job()
    job_ctx = SortingHatContext(ctx.user, job.id)

    # Create an empty transaction to log which job
    # will generate the enroll transactions.
    trxl = TransactionsLog.open('affiliate', job_ctx)

    for chunk in _iter_split(uuids, size=MAX_CHUNK_SIZE):
        for rec in engine.recommend('affiliation', chunk):
            affiliated, errs = _affiliate_individual(job_ctx, rec.key, rec.options)
            results[rec.key] = affiliated
            errors.extend(errs)

    trxl.close()

    return job_result


@django_rq.job
def unify(ctx, source_uuids, target_uuids, criteria):
    """Unify a set of individuals by merging them using matching recommendations.

    This function automates the identities unify process obtaining
    a list of recommendations where matching individuals can be merged.
    After that, matching individuals are merged.
    This job returns a list with the individuals which have been merged
    and the errors generated during this process.

    Individuals both for `source_uuids` and `target_uuids` are defined by
    any of their valid keys or UUIDs. When the parameter `target_uuids` is empty,
    the matches and the later merges will take place comparing the identities
    from the individuals in `source_uuids` against all the identities on the registry.

    :param ctx: context where this job is run
    :param source_uuids: list of individuals identifiers to look matches for
    :param target_uuids: list of individuals identifiers where to look for matches
    :param criteria: list of fields which the unify will be based on
        (`email`, `name` and/or `username`)

    :returns: a list with the individuals resulting from merge operations
        and the errors found running the job
    """
    def _group_recommendations(recs):
        """Calculate unique sets of identities from matching recommendations.

        For instance, given a list of matching groups like
        A = {A, B}; B = {B,A,C}, C = {C,} and D = {D,} the output
        for keys A, B and C will be the group {A, B, C}. As D has no matches,
        it won't be included in any group and it won't be returned.

        :param recs: recommendations of matching identities

        :returns: a list including unique groups of matches
        """
        groups = []
        for group_key in recs:
            g_uuids = pandas.Series(recs[group_key])
            g_uuids = g_uuids.append(pandas.Series([group_key]))
            g_uuids = list(g_uuids.sort_values().unique())
            if (len(g_uuids) > 1) and (g_uuids not in groups):
                groups.append(g_uuids)
        return groups

    results = []
    errors = []

    job_result = {
        'results': results,
        'errors': errors
    }

    engine = RecommendationEngine()

    # Create a new context to include the reference
    # to the job id that will perform the transaction.
    job = rq.get_current_job()
    job_ctx = SortingHatContext(ctx.user, job.id)

    trxl = TransactionsLog.open('unify', job_ctx)

    match_recs = {}
    for rec in engine.recommend('matches', source_uuids, target_uuids, criteria):
        match_recs[rec.key] = list(rec.options)

    match_groups = _group_recommendations(match_recs)

    # Apply the merge of the matching identities
    for group in match_groups:
        uuid = group[0]
        result = group[1:]
        merged_to, errs = _merge_individuals(job_ctx, uuid, result)
        if merged_to:
            results.append(merged_to)
        errors.extend(errs)

    trxl.close()

    return job_result


def _merge_individuals(job_ctx, source_indv, target_indvs):
    """Merge a set of individuals.

    Returns a tuple with two elements: list of the uuids from
    the individuals who were merged; list of errors found
    during the process.

    :param job_ctx: job context
    :param source_indv: valid individual identifier where
        the rest of individuals will be merged to
    :param target_indvs: list of identifiers of the individuals
        who will be merged with the source individual

    :returns: tuple with the uuid from the individual resulting from the merge
     operation (if any), and list of errors found during the process
    """
    errors = []

    try:
        to_indv = merge(job_ctx, target_indvs, source_indv)
    except EqualIndividualError:
        # When source identity is already part of the destination, the merge is not applied
        to_indv = None
        pass
    except BaseError as exc:
        to_indv = None
        errors.append(str(exc))

    to_indv = to_indv.mk if to_indv else None

    return to_indv, errors


def _affiliate_individual(job_ctx, uuid, organizations):
    """Affiliate an individual to a list of organizations.

    Returns a tuple with two elements: list of the organizations
    the individual was enrolled to; list of the errors found
    during the process.

    :param job_ctx: job context
    :param uuid: valid individual identifier
    :param organizations: list of organization names

    :returns: tuple with the organizations affiliated to the i
    """
    affiliated = []
    errors = []

    for name in organizations:
        try:
            enroll(job_ctx, uuid, name)
        except BaseError as exc:
            errors.append(str(exc))
        else:
            affiliated.append(name)
    return affiliated, errors


def _iter_split(iterator, size=None):
    """Split an iterator in chunks of the same size.

    When size is `None` the iterator will only return
    one chunk.

    :param iterator: iterator to split
    :param size: size of the chunk;

    :returns: generator of chunks
    """
    # This code is based on Ashley Waite's answer to StackOverflow question
    # "split a generator/iterable every n items in python (splitEvery)"
    # (https://stackoverflow.com/a/44320132).
    while True:
        slice_iter = itertools.islice(iterator, size)
        peek = next(slice_iter)
        yield itertools.chain([peek], slice_iter)