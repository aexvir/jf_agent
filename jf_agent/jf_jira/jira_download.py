from tqdm import tqdm
import queue
import threading
import sys
import string
import json
import logging

from jira.resources import dict2resource
from jira.exceptions import JIRAError
from jira.utils import json_loads

from jf_agent import diagnostics, agent_logging

logger = logging.getLogger(__name__)


# Returns an array of User dicts
@diagnostics.capture_timing()
@agent_logging.log_entry_exit(logger)
def download_users(jira_connection, gdpr_active):
    print('downloading jira users... ', end='', flush=True)

    jira_users = _search_all_users(jira_connection, gdpr_active)

    # Some jira instances won't return more than one page of
    # results.  If we have seen exactly 1000 results, try
    # searching a different way
    if len(jira_users) == 1000:
        jira_users = _users_by_letter(jira_connection, gdpr_active)

    print('✓')
    return jira_users


# Returns an array of Field dicts
@diagnostics.capture_timing()
@agent_logging.log_entry_exit(logger)
def download_fields(jira_connection, include_fields, exclude_fields):

    print('downloading jira fields... ', end='', flush=True)

    filters = []
    if include_fields:
        filters.append(lambda field: field['id'] in include_fields)
    if exclude_fields:
        filters.append(lambda field: field['id'] not in exclude_fields)

    fields = [field for field in jira_connection.fields() if all(filt(field) for filt in filters)]

    print('✓')
    return fields


# Returns an array of Resolutions dicts
@diagnostics.capture_timing()
@agent_logging.log_entry_exit(logger)
def download_resolutions(jira_connection):
    print('downloading jira resolutions... ', end='', flush=True)
    result = [r.raw for r in jira_connection.resolutions()]
    print('✓')
    return result


# Returns an array of IssueType dicts
@diagnostics.capture_timing()
@agent_logging.log_entry_exit(logger)
def download_issuetypes(jira_connection, project_ids):
    '''
    For Jira next-gen projects, issue types can be scoped to projects.
    For issue types that are scoped to projects, only extract the ones
    in the extracted projects.
    '''
    print('downloading jira issue types... ', end='', flush=True)
    result = []
    for it in jira_connection.issue_types():
        if 'scope' in it.raw and it.raw['scope']['type'] == 'PROJECT':
            if it.raw['scope']['project']['id'] in project_ids:
                result.append(it.raw)
        else:
            result.append(it.raw)
    print('✓')
    return result


# Returns an array of LinkType dicts
@diagnostics.capture_timing()
@agent_logging.log_entry_exit(logger)
def download_issuelinktypes(jira_connection):
    print('downloading jira issue link types... ', end='', flush=True)
    result = [lt.raw for lt in jira_connection.issue_link_types()]
    print('✓')
    return result


# Returns an array of Priority dicts
@diagnostics.capture_timing()
@agent_logging.log_entry_exit(logger)
def download_priorities(jira_connection):
    print('downloading jira priorities... ', end='', flush=True)
    result = [p.raw for p in jira_connection.priorities()]
    print('✓')
    return result


# Each project has a list of versions.
# Returns an array of Project dicts, where each one is agumented with an array of associated Version dicts.
@diagnostics.capture_timing()
@agent_logging.log_entry_exit(logger)
def download_projects_and_versions(
    jira_connection, include_projects, exclude_projects, include_categories, exclude_categories
):

    print('downloading jira projects... ', end='', flush=True)

    filters = []
    if include_projects:
        filters.append(lambda proj: proj.key in include_projects)
    if exclude_projects:
        filters.append(lambda proj: proj.key not in exclude_projects)
    if include_categories:
        filters.append(lambda proj: proj.projectCategory.name in include_categories)
    if exclude_categories:
        filters.append(lambda proj: proj.projectCategory.name not in exclude_categories)

    all_projects = jira_connection.projects()
    projects = [proj for proj in all_projects if all(filt(proj) for filt in filters)]
    if not projects:
        raise Exception(
            'No Jira projects found that meet all the provided filters for project and project category. Aborting... '
        )

    print('✓')

    print(f'downloading jira project components... ', end='', flush=True)
    for p in projects:
        p.raw.update({'components': [c.raw for c in jira_connection.project_components(p)]})
    print('✓')

    print('downloading jira versions... ', end='', flush=True)
    result = [
        p.raw.update({'versions': [v.raw for v in jira_connection.project_versions(p)]}) or p.raw
        for p in projects
    ]
    print('✓')
    return result


# Boards and Sprints are many-to-many.
# Returns a 3-tuple:
#   - Array of board dicts
#   - Array of sprint dicts
#   - Array of board/sprint links
@diagnostics.capture_timing()
@agent_logging.log_entry_exit(logger)
def download_boards_and_sprints(jira_connection, project_ids):
    b_start_at = 0
    boards = []
    print('downloading jira boards... ', end='', flush=True)
    while True:
        # boards seem to come back in batches of 50 at most
        jira_boards = jira_connection.boards(startAt=b_start_at, maxResults=50)
        if not jira_boards:
            break
        b_start_at += len(jira_boards)

        # Some versions of Jira map boards to projects, some do not.
        # If this includes a "location" for boards, then only
        # include boards for the projects we're pulling
        boards.extend(
            [
                b
                for b in jira_boards
                if not hasattr(b, 'location')
                or str(getattr(b.location, 'projectId', '')) in project_ids
            ]
        )
    print('✓')

    links = []
    sprints = {}
    for b in tqdm(boards, desc='downloading jira sprints', file=sys.stdout):
        if b.raw['type'] != 'scrum':
            continue
        s_start_at = 0
        sprints_for_board = []
        while True:
            batch = None
            try:
                batch = jira_connection.sprints(board_id=b.id, startAt=s_start_at, maxResults=50)
            except JIRAError as e:
                # JIRA returns 500 errors for various reasons: board is
                # misconfigured; "falied to execute search"; etc.  Just
                # skip and move on
                if e.status_code == 500 or e.status_code == 404:
                    print(f"Couldn't get sprints for board {b.id}.  Skipping...")
                else:
                    raise

            if not batch:
                break
            s_start_at += len(batch)
            sprints_for_board.extend(batch)

        links.append({'board_id': b.id, 'sprint_ids': [s.id for s in sprints_for_board]})
        sprints.update({s.id: s for s in sprints_for_board})

    return [b.raw for b in boards], [s.raw for s in sprints.values()], links


def download_issue_batch(
    jira_connection, project_ids, include_fields, exclude_fields, issue_filter=None
):
    if not issue_filter:
        issue_filter = 'created is not empty'

    issue_jql = f'{issue_filter} and project in ({",".join(project_ids)}) order by id asc'
    issue_count = jira_connection.search_issues(issue_jql, fields='id', maxResults=1).total
    parallel_threads = 10
    issues_per_thread = -(-issue_count // parallel_threads)

    # Make threads to talk to Jira and write batches of issues to the queue
    q = queue.Queue()
    threads = [
        threading.Thread(
            target=_download_some_jira_issues,
            args=[
                i,
                issue_jql,
                include_fields,
                exclude_fields,
                jira_connection,
                i * issues_per_thread,
                (i + 1) * issues_per_thread,
                q,
            ],
        )
        for i in range(parallel_threads)
    ]
    for t in threads:
        t.start()

    encountered_issue_ids = set()
    with tqdm(desc='downloading jira issues', total=issue_count, file=sys.stdout) as prog_bar:
        # Read batches from queue
        finished = 0
        while finished < len(threads):
            batch = q.get()
            if batch is None:
                # found the marker that a thread is done
                finished += 1
                continue

            if isinstance(batch, BaseException):
                # thread had a problem; rethrow
                raise batch

            # issues sometimes appear at the end of one batch and again at the beginning of the next; de-dup them
            new_issues_this_batch = []
            for i in batch:
                if i['id'] not in encountered_issue_ids:
                    encountered_issue_ids.add(i['id'])
                    new_issues_this_batch.append(i)
            prog_bar.update(len(new_issues_this_batch))

            yield new_issues_this_batch

    for t in threads:
        t.join()


# Returns a dict with two items: 'existing' gives a list of all worklogs
# that currently exist; 'deleted' gives the list of worklogs that
# existed at some point previously, but have since been deleted
@diagnostics.capture_timing()
@agent_logging.log_entry_exit(logger)
def download_worklogs(jira_connection, issue_ids):
    print(f'downloading jira worklogs... ', end='', flush=True)
    updated = []
    since = 0
    while True:
        worklog_ids_json = jira_connection._get_json('worklog/updated', params={'since': since})
        updated_worklog_ids = [v['worklogId'] for v in worklog_ids_json['values']]

        resp = jira_connection._session.post(
            url=jira_connection._get_url('worklog/list'),
            data=json.dumps({'ids': updated_worklog_ids}),
        )
        try:
            worklog_list_json = json_loads(resp)
        except ValueError:
            print("Couldn't parse JIRA response as JSON: %s", resp.text)
            raise

        updated.extend([wl for wl in worklog_list_json if wl['issueId'] in issue_ids])
        if worklog_ids_json['lastPage']:
            break
        since = worklog_ids_json['until']

    print('✓')

    return {'existing': updated, 'deleted': []}


# Returns an array of CustomFieldOption items
@diagnostics.capture_timing()
@agent_logging.log_entry_exit(logger)
def download_customfieldoptions(jira_connection):
    print('downloading jira custom field options... ', end='', flush=True)
    try:
        meta = jira_connection.createmeta(projectIds=None, expand='projects.issuetypes.fields')
    except JIRAError:
        agent_logging.log_and_print(
            logger, logging.ERROR, f'Error calling createmeta JIRA endpoint', exc_info=True
        )
        return []

    # Custom values are buried deep in the createmeta response:
    #     projects -> issuetypes -> fields -> allowedValues
    opt_dict = {}
    for project in meta['projects']:
        for issue_type in project['issuetypes']:
            for field_key, field in issue_type['fields'].items():
                # we've seen variance in the location of the key field -- use it if it's available (OJ-5279)
                if 'key' in field:
                    field_key = field['key']
                # same field may end up in multiple issue types (bug, task, etc),
                # so check if we've already added it
                if field_key not in opt_dict and _is_option_field(field):
                    opt_dict[field_key] = field['allowedValues']

    result = [{'field_key': k, 'raw_json': v} for k, v in opt_dict.items()]
    print('✓')
    return result


# return True if this field is either a single or multi select option
# field_meta is a dict that comes from raw field json
def _is_option_field(field_meta):
    schema = field_meta['schema']
    is_option_field = schema['type'] == 'option' or (
        'items' in schema and schema['items'] == 'option'
    )
    return is_option_field


def _jira_user_key(user_dict):
    return user_dict.get('key', user_dict.get('accountId', 'unknown_key'))


def _search_all_users(jira_connection, gdpr_active):
    jira_users = {}
    start_at = 0

    # different jira versions / different times, the way to list all users has changed. Try a few.
    for q in [None, '', '%', '@']:

        # iterate through pages of results
        while True:
            users = _search_users(
                jira_connection, gdpr_active, query=q, start_at=start_at, include_inactive=True
            )
            if not users:
                # we're done, no more pages
                break

            # add this page of results and get the next page
            jira_users.update({_jira_user_key(u): u for u in users})
            start_at += len(users)

        # found users; no need to try other query techniques
        if jira_users:
            return list(jira_users.values())

    # no users found
    return []


def _users_by_letter(jira_connection, gdpr_active):
    jira_users = {}
    for letter in string.ascii_lowercase:
        jira_users.update(
            {
                _jira_user_key(u): u
                for u in _search_users(
                    jira_connection,
                    gdpr_active,
                    query=f'{letter}.',
                    include_inactive=True,
                    include_active=False,
                )
            }
        )
        jira_users.update(
            {
                _jira_user_key(u): u
                for u in _search_users(
                    jira_connection,
                    gdpr_active,
                    query=f'{letter}.',
                    include_inactive=False,
                    include_active=True,
                )
            }
        )
    return list(jira_users.values())


def _search_users(
    jira_connection,
    gdpr_active,
    query,
    start_at=0,
    max_results=1000,
    include_active=True,
    include_inactive=False,
):
    if query is None:
        # use new endpoint that doesn't take a query.  This may not exist in some instances.
        try:
            return jira_connection._get_json(
                'users/search', {'startAt': start_at, 'maxResults': max_results}
            )
        except JIRAError:
            return []

    if gdpr_active:
        # jira_connection.search_users creates a query that is no longer accepted on
        # GDPR-compliant Jira instances.  Construct the right query by hand
        params = {
            'startAt': start_at,
            'maxResults': max_results,
            'query': query,
            'includeActive': include_active,
            'includeInactive': include_inactive,
        }
        return jira_connection._get_json('user/search', params)

    return [
        u.raw
        for u in jira_connection.search_users(
            query,
            startAt=start_at,
            maxResults=max_results,
            includeInactive=include_inactive,
            includeActive=include_active,
        )
    ]


@agent_logging.log_entry_exit(logger)
def _download_some_jira_issues(
    i, issue_jql, include_fields, exclude_fields, jira_connection, start_at, end_at, q
):
    # Empirically, Jira cloud won't return more than 100 issues at a
    # time even if you ask for more, but on-premise can do 1000
    #
    # This could be brittle, either with changes in Jira cloud, or with
    # different on-premise servers configured differently
    batch_size = 1000 if jira_connection.deploymentType == 'Server' else 100
    total_issues = end_at

    try:
        # pull batches and bulk load each into our database
        while start_at < min(end_at, total_issues):
            issues = _get_jira_issues_batch(
                issue_jql, include_fields, exclude_fields, jira_connection, start_at, batch_size
            )

            total_issues = issues.total
            issues_retrieved = len(issues)
            start_at += issues_retrieved
            if issues_retrieved == 0:
                agent_logging.log_and_print(
                    logger,
                    logging.WARNING,
                    f'Thread {i} - Jira issue downloaded might be stuck; asked for {batch_size} issues starting at {start_at}, '
                    f'ending at ending at {min(end_at, total_issues)}, got none back.  Trying again...',
                )

            raw_issues = [i.raw for i in issues]

            # TODO: configurable way to scrub things out of raw_issues here before we write them out.

            q.put(raw_issues)

        # sentinel to mark that this thread finished
        q.put(None)

    except BaseException as e:
        agent_logging.log_and_print(
            logger, logging.ERROR, f'Thread {i} - Jira issue downloader FAILED', exc_info=True
        )
        q.put(e)


def _get_jira_issues_batch(
    issue_jql, include_fields, exclude_fields, jira_connection, start_at, max_results
):
    get_changelog = True

    fieldspec = ','.join(include_fields) if include_fields else '*all'
    for f in exclude_fields:
        fieldspec += f',-{f}'

    while max_results > 0:
        search_kwargs = {
            'fields': fieldspec,
            'expand': 'renderedFields',
            'startAt': start_at,
            'maxResults': max_results,
        }
        if get_changelog:
            search_kwargs['expand'] += ',changelog'

        try:
            return _expand_changelog(
                jira_connection.search_issues(issue_jql, **search_kwargs), jira_connection
            )
        except JIRAError:
            agent_logging.log_and_print(
                logger, logging.ERROR, 'JIRAError, reducing batch size', exc_info=True
            )
            ## back off the max results requested to try and avoid the error
            max_results = int(max_results / 2)
            if max_results == 0:
                ## if we hit 0, then there's an issue we can't request with a changelog, fetch
                ## it without a change log and continue
                get_changelog = False
                max_results = 1


# Sometimes the results of issue search has an incomplete changelog.  Fill it in if so.
def _expand_changelog(jira_issues, jira_connection):
    for i in jira_issues:
        changelog = getattr(i, 'changelog', None)
        if changelog and changelog.total != changelog.maxResults:
            # expand the changelog
            start_at = changelog.maxResults
            batch_size = 100
            while start_at < changelog.total:
                more_cls = jira_connection._get_json(
                    f'issue/{i.id}/changelog', {'startAt': start_at, 'maxResults': batch_size}
                )['values']
                changelog.histories.extend(dict2resource(i) for i in more_cls)
                i.raw['changelog']['histories'].extend(more_cls)
                start_at += len(more_cls)
    return jira_issues