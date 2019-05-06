from flask import make_response
from flask import request
from flask import redirect
from flask import abort
from flask import render_template
from flask import jsonify
from flask import g

import json
import os
import sys
import re
from time import time


from app import app
from app import db
from app import logger

from sqlalchemy import sql

from data.funders import funder_names
from journal import Journal
from topic import Topic
from institution import Institution
from transformative_agreement import TransformativeAgreement
from util import str2bool
from util import normalize_title
from util import clean_doi
from util import is_doi
from util import is_issn
from util import get_sql_answer

def json_dumper(obj):
    """
    if the obj has a to_dict() function we've implemented, uses it to get dict.
    from http://stackoverflow.com/a/28174796
    """
    try:
        return obj.to_dict()
    except AttributeError:
        return obj.__dict__


def json_resp(thing):
    json_str = json.dumps(thing, sort_keys=True, default=json_dumper, indent=4)

    if request.path.endswith(".json") and (os.getenv("FLASK_DEBUG", False) == "True"):
        logger.info(u"rendering output through debug_api.html template")
        resp = make_response(render_template(
            'debug_api.html',
            data=json_str))
        resp.mimetype = "text/html"
    else:
        resp = make_response(json_str, 200)
        resp.mimetype = "application/json"
    return resp


def abort_json(status_code, msg):
    body_dict = {
        "HTTP_status_code": status_code,
        "message": msg,
        "error": True
    }
    resp_string = json.dumps(body_dict, sort_keys=True, indent=4)
    resp = make_response(resp_string, status_code)
    resp.mimetype = "application/json"
    abort(resp)



@app.after_request
def after_request_stuff(resp):

    #support CORS
    resp.headers['Access-Control-Allow-Origin'] = "*"
    resp.headers['Access-Control-Allow-Methods'] = "POST, GET, OPTIONS, PUT, DELETE, PATCH"
    resp.headers['Access-Control-Allow-Headers'] = "origin, content-type, accept, x-requested-with"

    # remove session
    db.session.remove()

    # without this jason's heroku local buffers forever
    sys.stdout.flush()

    return resp



@app.before_request
def stuff_before_request():

    g.request_start_time = time()

    # don't redirect http api in some cases
    if request.url.startswith("http://api."):
        return
    if "staging" in request.url or "localhost" in request.url:
        return

    # redirect everything else to https.
    new_url = None
    try:
        if request.headers["X-Forwarded-Proto"] == "https":
            pass
        elif "http://" in request.url:
            new_url = request.url.replace("http://", "https://")
    except KeyError:
        # logger.info(u"There's no X-Forwarded-Proto header; assuming localhost, serving http.")
        pass

    if new_url:
        return redirect(new_url, 301)  # permanent


@app.route("/test", methods=["GET"])
def get_example():
    return jsonify({"results": "hi"})

@app.route('/', methods=["GET", "POST"])
def base_endpoint():
    return jsonify({
        "version": "0.0.1",
        "msg": "Don't panic"
    })


@app.route("/autocomplete/topics/name/<q>", methods=["GET"])
def topics_title_search(q):
    ret = []

    query_for_search = re.sub(r'[!\'()|&]', ' ', q).strip()
    if query_for_search:
        query_for_search = re.sub(r'\s+', ' & ', query_for_search)
        query_for_search += ':*'

    command = """with together as (
			select
            topic,
            sum(num_articles_3years) as num_total_3years
        	from bq_scimago_issnl_topics group by topic)
            select 
                topic,
                num_total_3years, 
                ts_rank_cd(to_tsvector('only_stop_words', topic), query, 1) AS rank,
                num_total_3years + 100000 * ts_rank_cd(to_tsvector('only_stop_words', topic), query, 1) as score
            from together, to_tsquery('only_stop_words', '{query_for_search}') query
            where to_tsvector('only_stop_words', topic) @@ query
            order by num_total_3years + 100000 * ts_rank_cd(to_tsvector('only_stop_words', topic), query, 1) desc
            limit 10
        """.format(query_for_search=query_for_search)
    res = db.session.connection().execute(sql.text(command))
    rows = res.fetchall()
    for row in rows:
        ret.append({
            "topic": row[0],
            "num_total_3years": row[1],
            "fulltext_rank": row[2],
            "score": row[3],
        })
    return jsonify({ "list": ret, "count": len(ret)})

@app.route("/autocomplete/journals/name/<q>", methods=["GET"])
def journal_title_search(q):
    ret = []

    query_for_search = re.sub(r'[!\'()|&]', ' ', q).strip()
    if query_for_search:
        query_for_search = re.sub(r'\s+', ' & ', query_for_search)
        query_for_search += ':*'

    command = """select 
                issnl, 
                num_articles_since_2018, 
                title, 
                prop_cc_by_since_2018,
                ts_rank_cd(to_tsvector('only_stop_words', title), query, 1) AS rank,
                num_articles_since_2018 + 10000 * ts_rank_cd(to_tsvector('only_stop_words', title), query, 1) as score
            
            from bq_our_journals_issnl, to_tsquery('only_stop_words', '{query_for_search}') query
            where to_tsvector('only_stop_words', title) @@ query
            order by num_articles_since_2018 + 10000 * ts_rank_cd(to_tsvector('only_stop_words', title), query, 1) desc
            limit 10
    """.format(query_for_search=query_for_search)
    res = db.session.connection().execute(sql.text(command))
    rows = res.fetchall()
    for row in rows:
        ret.append({
            "id": row[0],
            "num_articles_since_2018": row[1],
            "name": row[2],
            "prop_cc_by_since_2018": row[3],
            "fulltext_rank": row[4],
            "score": row[5],
        })
    return jsonify({ "list": ret, "count": len(ret)})


@app.route("/transformative-agreements", methods=["GET"])
def transformative_agreements_get():
    transformative_agreements = TransformativeAgreement.query.all()
    return jsonify({"list": [ta.to_dict_short() for ta in transformative_agreements], "count": len(transformative_agreements)})

@app.route("/transformative-agreement/<id>", methods=["GET"])
def transformative_agreement_lookup(id):
    my_ta = TransformativeAgreement.query.get(id)
    return jsonify(my_ta.to_dict())


@app.route("/institution/<id>", methods=["GET"])
def institution_lookup(id):
    my_institution = Institution.query.filter(Institution.grid_id == id).first()
    return jsonify(my_institution.to_dict())


@app.route("/funder/<id>", methods=["GET"])
def funder_lookup(id):

    matches = [funder for funder in funder_names if str(funder["id"]) == str(id)]
    name = None
    if matches:
        name = matches[0]["name"]

    return jsonify({"id": id, "name": name})


@app.route("/autocomplete/institutions/name/<q>", methods=["GET"])
def institutions_name_autocomplete(q):
    institutions = Institution.query.filter(Institution.org_name.ilike(u'%{}%'.format(q))).order_by(Institution.num_papers.desc()).limit(10).all()
    return jsonify({"list": [inst.to_dict() for inst in institutions], "count": len(institutions)})


@app.route("/autocomplete/funders/name/<q>", methods=["GET"])
def funders_name_search(q):

    ret = [funder for funder in funder_names if q.lower() in funder["alternate_names"].lower()]

    return jsonify({"list": ret, "count": len(ret)})


@app.route("/journal/<issnl_query>", methods=["GET"])
def journal_issnl_get(issnl_query):
    funder_id = request.args.get("funder", None)
    institution_id = request.args.get("institution", None)
    if institution_id and "grid" in institution_id:
        institution = Institution.query.get(institution_id)
    else:
        institution = None

    my_journal = Journal.query.filter(Journal.issnl == issnl_query).first()
    return jsonify(my_journal.to_dict_full(funder_id, institution))


@app.route("/topic/<topic_query>", methods=["GET"])
def topic_get(topic_query):
    funder_id = request.args.get("funder", None)
    institution_id = request.args.get("institution", None)
    if institution_id and "grid" in institution_id:
        institution = Institution.query.get(institution_id)
    else:
        institution = None

    include_uncompliant = False
    if "include-uncompliant" in request.args:
        include_uncompliant = str2bool(request.args.get("include-uncompliant", "true"))
        if request.args.get("include-uncompliant") == '':
            include_uncompliant = True

    if include_uncompliant:
        limit = 50  # won't need to filter any out
    else:
        limit = 1000

    topic_hits = Topic.query.filter(Topic.topic == topic_query).order_by(Topic.num_articles_3years.desc()).limit(limit)
    our_journals = Journal.query.filter(Journal.issnl.in_([t.issnl for t in topic_hits])).all()
    responses = []
    for this_journal in our_journals:
        if include_uncompliant or this_journal.is_compliant(funder_id, institution):
            response = this_journal.to_dict_journal_row(funder_id, institution)
            responses.append(response)
    responses = sorted(responses, key=lambda k: k['num_articles_since_2018'], reverse=True)[:50]
    return jsonify({ "list": responses, "count": len(responses)})



@app.route("/search/journals/<journal_query>", methods=["GET"])
def search_journals_get(journal_query):
    funder_id = request.args.get("funder", None)
    institution_id = request.args.get("institution", None)
    if institution_id and "grid" in institution_id:
        institution = Institution.query.get(institution_id)
    else:
        institution = None

    include_uncompliant = False
    if "include-uncompliant" in request.args:
        include_uncompliant = str2bool(request.args.get("include-uncompliant", "true"))
        if request.args.get("include-uncompliant") == '':
            include_uncompliant = True

    if include_uncompliant:
        limit = 50  # won't need to filter any out
    else:
        limit = 1000

    response = []

    query_for_search = re.sub(r'[!\'()|&]', ' ', journal_query).strip()
    if query_for_search:
        query_for_search = re.sub(r'\s+', ' & ', query_for_search)
        query_for_search += ':*'

    command = """select 
                issnl, 
                ts_rank_cd(to_tsvector('only_stop_words', title), query, 1) AS rank,
                num_articles + 10000 * ts_rank_cd(to_tsvector('only_stop_words', title), query, 1) as score
            from bq_our_journals_issnl, to_tsquery('only_stop_words', '{query_for_search}') query
            where to_tsvector('only_stop_words', title) @@ query
            order by num_articles_since_2018 + 10000 * ts_rank_cd(to_tsvector('only_stop_words', title), query, 1) desc
            limit {limit}
    """.format(query_for_search=query_for_search, limit=limit)
    res = db.session.connection().execute(sql.text(command))
    rows = res.fetchall()

    issnls = [row[0] for row in rows]
    our_journals = Journal.query.filter(Journal.issnl.in_(issnls)).all()
    # print our_journals
    responses = []
    for this_journal in our_journals:
        if include_uncompliant or this_journal.is_compliant(funder_id, institution):
            response = this_journal.to_dict_journal_row(funder_id, institution)
            matching_score_row = [row for row in rows if row[0]==this_journal.issnl][0]
            response["fulltext_rank"] = matching_score_row[1]
            response["score"] = matching_score_row[2]
            responses.append(response)

    responses = sorted(responses, key=lambda k: k['score'], reverse=True)[:50]

    return jsonify({ "list": responses, "count": len(responses)})

@app.route("/unpaywall-metrics/subscriptions", methods=["GET"])
def unpaywall_journals_subscriptions_get():
    responses = []

    command = """select cdl_subscription_summary_mv.issnl, journal_name, from_date, cdl_subscription_summary_mv.num_dois, num_oa, oa_rate, issns,
                cdl_subscription_summary_mv.num_dois  as score,
                proportion_is_oa, proportion_repository_hosted, proportion_publisher_hosted
            from cdl_subscription_summary_mv, cdl_subscription_oa_counts_mv
            where cdl_subscription_summary_mv.issnl = cdl_subscription_oa_counts_mv.issnl
            order by cdl_subscription_summary_mv.num_dois desc
                """
    res = db.session.connection().execute(sql.text(command), bind=db.get_engine(app, 'unpaywall_db'))
    rows = res.fetchall()

    for row in rows:
        to_dict = {
            "issnl": row[0],
            "journal_name": row[1],
            "publisher": "Elsevier",
            "subscription_start_date": row[2].isoformat(),
            "num_dois": row[3],
            "num_oa": row[4],
            "proportion_oa": row[8],
            "proportion_repository_hosted": row[9],
            "proportion_publisher_hosted": row[10],
            "issns": row[6],
            "score": row[7]

        }
        responses.append(to_dict)

    responses = sorted(responses, key=lambda k: k['score'], reverse=True)

    return jsonify({ "list": responses, "count": len(responses)})

@app.route("/unpaywall-metrics/subscriptions/name/<q>", methods=["GET"])
def unpaywall_journals_autocomplete_journals(q):
    ret = []

    query_for_search = re.sub(r'[!\'()|&]', ' ', q).strip()
    if query_for_search:
        query_for_search = re.sub(r'\s+', ' & ', query_for_search)
        query_for_search += ':*'

    command = """select cdl_subscription_summary_mv.issnl, journal_name, from_date, cdl_subscription_summary_mv.num_dois, num_oa, oa_rate, issns,
                ts_rank_cd(to_tsvector('only_stop_words', journal_name), query, 1) as text_rank,
                cdl_subscription_summary_mv.num_dois + 10000 * ts_rank_cd(to_tsvector('only_stop_words', journal_name), query, 1) as score,
                proportion_is_oa, proportion_repository_hosted, proportion_publisher_hosted
            from cdl_subscription_summary_mv, to_tsquery('only_stop_words', '{query_for_search}') query, cdl_subscription_oa_counts_mv
            where to_tsvector('only_stop_words', journal_name) @@ query
            and cdl_subscription_summary_mv.issnl = cdl_subscription_oa_counts_mv.issnl
            order by cdl_subscription_summary_mv.num_dois + 10000 * ts_rank_cd(to_tsvector('only_stop_words', journal_name), query, 1) desc
            limit 10
    """.format(query_for_search=query_for_search)
    print command
    res = db.session.connection().execute(sql.text(command), bind=db.get_engine(app, 'unpaywall_db'))
    rows = res.fetchall()

    responses = []
    for row in rows:
        to_dict = {
            "issnl": row[0],
            "journal_name": row[1],
            "publisher": "Elsevier",
            "subscription_start_date": row[2].isoformat(),
            "num_dois": row[3],
            "num_oa": row[4],
            "proportion_oa": row[9],
            "proportion_repository_hosted": row[10],
            "proportion_publisher_hosted": row[11],
            "issns": row[6],
            "text_rank": row[7],
            "score": row[8]
        }
        responses.append(to_dict)

    responses = sorted(responses, key=lambda k: k['text_rank'], reverse=True)  # or could use score

    return jsonify({ "list": responses, "count": len(responses)})

@app.route("/unpaywall-metrics/subscription/issn/<q>", methods=["GET"])
def unpaywall_journals_issn(q):
    ret = []

    query_for_search = q

    command = """
        select cdl_subscription_summary_mv.issnl, journal_name, from_date, cdl_subscription_summary_mv.num_dois, num_oa, oa_rate, issns,
                        cdl_subscription_summary_mv.num_dois  as score,
                        proportion_is_oa, proportion_repository_hosted, proportion_publisher_hosted
                    from cdl_subscription_summary_mv, cdl_subscription_oa_counts_mv
                    where cdl_subscription_summary_mv.issnl = cdl_subscription_oa_counts_mv.issnl
                    and array['{query_for_search}'] <@ issns
                order by score desc
                limit 10
    """.format(query_for_search=query_for_search)
    res = db.session.connection().execute(sql.text(command), bind=db.get_engine(app, 'unpaywall_db'))

    row = res.first()  # just get one

    to_dict = {
        "issnl": row[0],
        "journal_name": row[1],
        "publisher": "Elsevier",
        "subscription_start_date": row[2].isoformat(),
        "num_dois": row[3],
        "num_oa": row[4],
        "proportion_oa": row[8],
        "proportion_repository_hosted": row[9],
        "proportion_publisher_hosted": row[10],
        "issns": row[6],
        "score": row[7]
    }

    return jsonify({ "response": to_dict})



@app.route("/unpaywall-metrics/breakdown", methods=["GET"])
def unpaywall_metrics_breakdown():
    q = """
     SELECT 
    count(id) FILTER (where oa_status = 'closed') as num_closed,
    count(id) FILTER (where has_green and oa_status in ('hybrid', 'bronze', 'gold')) as num_has_repo_and_has_publisher,
    count(id) FILTER (where has_green and not oa_status in ('hybrid', 'bronze', 'gold')) as num_has_repo_and_not_publisher,
    count(id) FILTER (where (not has_green) and oa_status in ('hybrid', 'bronze', 'gold')) as num_not_repo_and_has_publisher,
    count(id) as num_total
   FROM cdl_dois_with_attributes_mv
  where published_date is not null 
   """
    article_query_rows = db.engine.execute(sql.text(q)).fetchall()
    article_numbers = article_query_rows[0]
    q = "select count(*) from cdl_journals"
    num_journals = get_sql_answer(db, q)

    response = {
        "article_breakdown": {
            "num_closed": article_numbers[0],
            "num_has_repository_hosted_and_has_publisher_hosted": article_numbers[1],
            "num_has_repository_hosted_and_not_publisher_hosted": article_numbers[2],
            "num_not_repository_hosted_and_has_publisher_hosted": article_numbers[3]
        },
        "num_articles_total": article_numbers[4],
        "num_journals_total": num_journals,
    }
    return jsonify(response)

def build_oa_filter():
    oa_filter = ""
    if request.args.get("oa_host", None):
        oa_host_text = request.args.get("oa_host", "")
        if "publisher" in oa_host_text:
            oa_filter = u" and oa_status in ('hybrid', 'bronze', 'gold') "
        elif "repository" in oa_host_text:
            oa_filter = u" and has_green "
        elif oa_host_text == "any":
            oa_filter = u" and oa_status != 'closed' "
    return oa_filter

def build_text_filter():
    text_filter = ""
    if request.args.get("q", None):
        text_query = request.args.get("q", None)
        if text_query:
            if is_issn(text_query):
                text_filter = u" and issnl = '{}' ".format(text_query)
            elif is_doi(text_query):
                text_filter = u" and id = '{}' ".format(clean_doi(text_query))
            else:
                text_filter = u" and article_title ilike '%{}%' ".format(text_query)
    return text_filter


@app.route("/unpaywall-metrics/articles/count", methods=["GET"])
def unpaywall_metrics_articles_count():

    command = """
            select count(id) from cdl_dois_with_attributes_mv
            where published_date is not null
            {text_filter}
            {oa_filter}
        """.format(text_filter=build_text_filter(),
                   oa_filter=build_oa_filter())

    # print command
    res = db.session.connection().execute(sql.text(command), bind=db.get_engine(app, 'unpaywall_db'))
    row = res.first()

    return jsonify({"count": row[0]})


@app.route("/unpaywall-metrics/articles", methods=["GET"])
def unpaywall_metrics_articles_paged():

    # page starts at 1 not 0
    if request.args.get("page"):
        page = int(request.args.get("page"))
    else:
        page = 1

    if request.args.get("pagesize"):
        pagesize = int(request.args.get("pagesize"))
    else:
        pagesize = 20
    if pagesize > 1000:
        abort_json(400, u"pagesize too large; max 1000")

    offset = (page - 1) * pagesize

    command = """
        select pub.response_jsonb from pub where id in
            (
            select id from cdl_dois_with_attributes_mv
            where published_date is not null
            {text_filter}
            {oa_filter}
            order by published_date desc 
            limit {pagesize}
            offset {offset}
            )
        """.format(pagesize=pagesize,
                   offset=offset,
                   text_filter=build_text_filter(),
                   oa_filter=build_oa_filter())

    res = db.session.connection().execute(sql.text(command), bind=db.get_engine(app, 'unpaywall_db'))
    rows = res.fetchall()
    responses = [row[0] for row in rows]

    return jsonify({"page": page, "list": responses})





if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=True, threaded=True)

















