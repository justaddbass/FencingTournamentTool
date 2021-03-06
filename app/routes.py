from datetime import datetime
from urllib.parse import urlparse
import json
from json import JSONDecodeError
import math
from time import time
import re

from sqlalchemy import func
from flask import render_template, flash, redirect, url_for, request
from flask_login import login_user, logout_user, current_user, login_required
from app import app, db, cache
import flask_profiler

from app.forms import *
from app.models import *
from app.utils import generate_tournament, quicksort, Score, is_valid_pair
from app.email import send_password_reset_email, send_prereg_email

TIMEOUT = 30  # seconds

def is_to_of_tournament(user, tournament):
    if current_user.is_anonymous:
        return False
    access = AccessTable.query.filter_by(
        user_id=user.id, tournament_id=tournament.id).first()
    return bool(access)


@app.route('/')
@app.route('/index')
def index():
    return render_template('index.html', title='Home')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(
            username=form.username.data.lower()).first()
        if user is None or not user.check_password(form.password.data):
            flash('Invalid username or password.')
            return redirect(url_for('login'))
        login_user(user, remember=form.remember_me.data)
        next_page = request.args.get('next')
        if not next_page or urlparse(next_page).netloc != '':
            next_page = url_for('index')
        return redirect(next_page)
    return render_template('login.html', title='Log In', form=form)


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))


@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    form = RegistrationForm()
    if form.validate_on_submit():
        user = User(username=form.username.data.lower(), email=form.email.data)
        user.set_password(form.password.data)
        db.session.add(user)
        db.session.commit()
        flash('You are now a registered user.')
        return redirect(url_for('login'))
    return render_template('register.html', title='Register', form=form)


@app.route('/user/<username>')
@login_required
def personal_user(username):
    user = User.query.filter_by(username=username).first_or_404()
    if user != current_user:
        return redirect(url_for('index'))
    q = db.session.query(User, AccessTable, Tournament)\
        .filter(AccessTable.user_id == user.id)\
        .filter(Tournament.id == AccessTable.tournament_id).distinct()
    q = [i for _, _, i in q]
    return render_template(
        'user.html',
        title=user.username,
        user=user,
        tournaments=q,
        public=False)


@app.route('/tournament/<int:tournament_id>')
@cache.cached(timeout=TIMEOUT)
def public_tournament(tournament_id):
    tournament = Tournament.query.get_or_404(tournament_id)
    events = tournament.events
    return render_template(
        'tournament.html', tournament=tournament, events=events, public=True)


@app.route('/explore')
def explore():
    tournaments = Tournament.query.all()
    return render_template(
        'explore.html', title='Explore', tournaments=tournaments, public=True)


@app.route('/create-tournament', methods=['GET', 'POST'])
@login_required
def create_tournament():
    user = User.query.filter_by(username=current_user.username).first()
    form = CreateTournamentForm()
    if form.validate_on_submit():
        tournament = Tournament(name=form.name.data.title())
        access = AccessTable(
            user_id=user.id, tournament_id=tournament.id, main_to=True)
        user.tournaments.append(access)
        tournament.organizers.append(access)
        db.session.add(tournament)
        db.session.commit()
        flash('Created new tournament.')
        return redirect(url_for('personal_user', username=current_user.username))
    return render_template(
        'create-tournament.html', title='Create Tournament', form=form)


@app.route(
    '/tournament/<int:tournament_id>/create-event', methods=['GET', 'POST'])
@login_required
def create_event(tournament_id):
    tournament = Tournament.query.get_or_404(tournament_id)
    if not is_to_of_tournament(current_user, tournament):
        return redirect(url_for('index'))
    form = CreateEventForm()
    if form.validate_on_submit():
        event = Event(
            name=form.name.data.title(),
            date=datetime.strptime(
                form.date.data.strftime('%m/%d/%Y'), '%m/%d/%Y'),
            weapon=form.weapon.data,
            tournament=tournament)
        tournament.events.append(event)
        db.session.add(event)
        db.session.commit()
        flash('Created new event.')
        return redirect(url_for('personal_user', username=current_user.username))
    return render_template(
        'create-event.html', tournament=tournament, form=form)

@app.route(
    '/tournament/<int:tournament_id>/create-event-default', methods=['GET', 'POST'])
@login_required
def create_event_default(tournament_id):
    return 'not implemented'


@app.route('/event/<int:event_id>/registration')
@cache.cached(timeout=TIMEOUT)
def registration(event_id):
    event = Event.query.get_or_404(event_id)
    title = 'Registration'
    return render_template(
        'registration-teams.html', title=title, event=event)


@app.route('/event/<int:event_id>/initial-seeding')
def initial_seeding(event_id):
    event = Event.query.get_or_404(event_id)
    teams = event.teams.filter_by(is_checked_in=True)
    public = not is_to_of_tournament(current_user, event.tournament)
    return render_template(
        'initial-seed-teams.html', event=event, teams=teams,
        public=public)


@app.route('/event/<int:event_id>/pool-results')
def pool_results(event_id):
    event = Event.query.get_or_404(event_id)
    public = True
    if is_to_of_tournament(current_user, event.tournament):
        public = False
    teams = db.engine.execute(
        """SELECT t.id, (t.victories*1.0 / (p.num_fencers - 1)) as winPercent
        FROM team t JOIN pool p ON t.Pool = p.id
        WHERE t.is_checked_in = 1 AND t.Event = {}
        ORDER BY winPercent DESC, t.indicator DESC, t.touches_scored DESC;
        """.format(event_id))
    teams = [[Team.query.get(i), j, ''] for (i, j) in teams]
    place = 0
    places = [[] for _ in range(len(teams))]
    for team in teams:
        if not places[place]:
            places[place].append(team)
        elif (team[0].indicator == places[place][0][0].indicator
                and team[0].touches_scored == places[place][0][0].touches_scored
                and team[1] == places[place][0][1]):
            places[place].append(team)
        else:
            place += 1
            places[place].append(team)
    place = 1
    for row in list(filter(lambda x: x, places)):
        if len(row) == 1:
            row[0][2] = str(place)
            place += 1
        else:
            for team in row:
                team[2] = str(place) + 'T'
            place += len(row)

    return render_template(
        'pool-results-teams.html',
        title='Pool Results',
        event=event,
        teams=teams,
        public=public)


@app.route('/event/<int:event_id>/pools')
@cache.cached(timeout=TIMEOUT)
def public_pools(event_id):
    event = Event.query.get_or_404(event_id)
    pools = event.pools
    results = {}
    teams = {}
    for pool in pools:
        if pool.pool_letter == 'O':
            teams[pool.poolNum] = pool.teams.order_by(Team.num_in_pool.asc())
            results[pool.poolNum] = {}
            for result in pool.results:
                team = Team.query.filter_by(
                    pool=pool, id=result.team.id).first()
                opponent_team = Team.query.filter_by(
                    id=result.opponent_team.id).first()
                results[pool.poolNum][str(team.num_in_pool)+str(opponent_team.num_in_pool)] = result
    return render_template(
        'pools.html',
        title='Pools',
        event=event,
        pools=pools,
        results=results,
        teams=teams)


@app.route('/event/<int:event_id>/pool-assignment')
@cache.cached(timeout=TIMEOUT)
def pool_assignment(event_id):
    event = Event.query.get_or_404(event_id)
    public = True
    if is_to_of_tournament(current_user, event.tournament):
        public = False
    pools = event.pools
    return render_template(
        'pool-assignments-teams.html',
        title='Pool Assignments',
        event=event,
        pools=pools,
        public=public)


@app.route('/event/<int:event_id>/de')
@cache.cached(timeout=TIMEOUT)
def public_de(event_id):
    event = Event.query.get_or_404(event_id)
    if(event.tableau_json == None):
        return "DEs have not been posted yet. Please check again later."
    return render_template(
        'de.html',
        title='DE',
        event=event,
        directElims=json.loads(event.tableau_json))


@app.route('/event/<int:event_id>/final')
@cache.cached(timeout=TIMEOUT)
def public_final(event_id):
    event = Event.query.get_or_404(event_id)
    #teams = event.teams.filter_by(is_checked_in=True).order_by(Team.final_place.asc()).all()
    teams = Team.query.filter(Team.event_id==event_id,Team.is_checked_in==True,Team.final_place.isnot(None)).order_by(Team.final_place.asc()).all()
    teams = [[team, i, ''] for (i, team) in enumerate(teams)]
    print("DE Teams: ", teams)
    if event.teams.count() > 12:
        q = db.engine.execute(
        """SELECT t.id, (t.victories*1.0 / (p.num_fencers - 1)) as winPercent
        FROM team t JOIN pool p ON t.Pool = p.id
        WHERE t.is_checked_in = 1 AND t.event= {}
        ORDER BY winPercent DESC, t.indicator DESC, t.touches_scored DESC
        """.format(event_id))
        pool_teams = [[Team.query.get(id), j, ''] for (id, j) in q]
        for team in pool_teams[12:]:
            teams.append(team)
        print("All teams: ", teams)
        place = 0
        places = [[] for _ in range(len(teams))]
        for team in teams[:12]:
            if not places[place]:
                places[place].append(team)
            elif team[0].de_indicator == places[place][0][0].de_indicator:
                places[place].append(team)
            else:
                place += 1
                places[place].append(team)
        place = 12
        for team in teams[12:]:
            if not places[place]:
                places[place].append(team)
            elif (team[0].indicator == places[place][0][0].indicator
                    and team[0].touches_scored == places[place][0][0].touches_scored
                    and team[1] == places[place][0][1]):
                places[place].append(team)
            else:
                place += 1
                places[place].append(team)
        place = 1
        for row in list(filter(lambda x: x, places)):
            if len(row) == 1:
                row[0][2] = str(place)
                place += 1
            else:
                for team in row:
                    team[2] = str(place) + 'T'
                place += len(row)
    return render_template('final.html', event=event, teams=teams)


@app.route('/tournament/<int:tournament_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_tournament(tournament_id):
    tournament = Tournament.query.filter_by(id=tournament_id).first()
    if not is_to_of_tournament(current_user, tournament):
        flash('You do not have permission to access this tournament.')
        return redirect(url_for('index'))
    form = AddTOForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data).first()
        if user == current_user:
            flash('You are already a TO of this tournament.')
            return redirect(url_for('edit_tournament', tournament_id=tournament_id))
        elif user is None:
            # TODO: send user email to register, requires a mail server
            return 'not implemented yet'
        access = AccessTable(
            user_id=user.id, tournament_id=tournament.id, main_to=False)
        user.tournaments.append(access)
        tournament.organizers.append(access)
        db.session.add(access)
        db.session.commit()
    events = tournament.events
    return render_template(
        'edit-tournament.html',
        title='Edit Tournament',
        tournament=tournament,
        events=events,
        form=form)


@app.route('/event/<int:event_id>/registration/edit', methods=['GET', 'POST'])
@login_required
def edit_registration(event_id):
    event = Event.query.get_or_404(event_id)
    tournament = event.tournament
    if not is_to_of_tournament(current_user, tournament):
        flash('You do not have permission to access this tournament.')
        return redirect(url_for('index'))

    teams = event.teams.all()
    form = AddTeamForm()
    if form.validate_on_submit():

        def add_fencer(name, position):
            return Fencer(first_name=name.split()[0].title(),
                          last_name=name.split()[1].title(),
                          team_position=position,
                          event=event)

        club = Club.query.filter_by(name=form.club.data).first()
        if club is None:
            club = Club(name=form.club.data)
        team = Team(name=form.teamName.data, is_checked_in=True)
        fencer_a = add_fencer(form.fencer_a.data, 'A')
        team.fencers.append(fencer_a)
        fencer_b = add_fencer(form.fencer_b.data, 'B')
        team.fencers.append(fencer_b)
        if form.fencer_c.data != '':
            fencer_c = add_fencer(form.fencer_c.data, 'C')
            team.fencers.append(fencer_c)
            db.session.add(fencer_c)
        else:  # create dummy fencer to fill c slot
            fencer_c = Fencer(
                first_name='',
                last_name='',
                team_position='C'
            )
            team.fencers.append(fencer_c)
            db.session.add(fencer_c)
        if form.fencer_d.data != '':
            fencer_d = add_fencer(form.fencer_d.data, 'D')
            team.fencers.append(fencer_d)
            db.session.add(fencer_d)
        else:  # create dummy fencer to fill d slot
            fencer_d = Fencer(
                first_name='',
                last_name='',
                team_position='D'
            )
            team.fencers.append(fencer_d)
            db.session.add(fencer_d)
        club.teams.append(team)
        event.teams.append(team)
        event.num_fencers_checked_in = Event.num_fencers_checked_in + 1
        event.num_fencers += 1
        db.session.add_all([club, team, fencer_a, fencer_b])
        db.session.commit()
        flash('Added team')
        return redirect(url_for('edit_registration', event_id=event_id))
    return render_template(
        'edit-registration-teams.html',
        form=form,
        teams=teams,
        event=event,
        allCheckedIn=(event.num_fencers_checked_in == event.num_fencers))


@app.route('/event/<int:event_id>/edit-pools')
@login_required
def edit_pools(event_id):
    event = Event.query.get_or_404(event_id)
    if not is_to_of_tournament(current_user, event.tournament):
        flash('You do not have permission to access this tournament.')
        return redirect(url_for('index'))
    pools = event.pools
    all_pools_done = False
    if event.is_stage(Stage.POOLS):  # check if all pools done to advance
        all_pools_done = True
        for pool in pools:
            if pool.state == 0 and pool.pool_letter != 'O':
                all_pools_done = False
    return render_template('edit-pools-teams.html', event=event, pools=pools, all_pools_done=all_pools_done)


@app.route(
    '/event/<int:event_id>/pool/<int:pool_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_pool(event_id, pool_id):
    pool = Pool.query.filter_by(id=pool_id).first()
    if not is_to_of_tournament(current_user, pool.event.tournament):
        flash('You do not have permission to access this tournament.')
        return redirect(url_for('index'))
    if request.method == "POST":
        for key, value in request.form.items():
            key = key.strip('result')
            score1 = Score(value)
            score2 = Score(request.form['result'+str(key[1])+str(key[0])])
            if not (score1 and score2 and is_valid_pair(score1, score2)):
                flash('Invalid score.')
                return redirect(
                    url_for('edit_pool', event_id=event_id, pool_id=pool_id))
            fencer = Fencer.query.filter_by(
                pool_id=pool_id, num_in_pool=key[0]).first()
            opponent = Fencer.query.filter_by(
                pool_id=pool_id, num_in_pool=key[1]).first()
            result = Result(
                pool_id=pool.id,
                event_id=event_id,
                fencer=fencer.id,
                team=fencer.team,
                fencer_score=score1.touches,
                opponent=opponent.id,
                opponent_team=opponent.team,
                fencer_win=score1.is_winner())
            fencer.victories = Fencer.victories + (1 if result.fencer_win else 0)
            fencer.touches_scored = Fencer.touches_scored + result.fencer_score
            fencer.indicator = Fencer.indicator + result.fencer_score
            opponent.touches_recieved = Fencer.touches_recieved + result.fencer_score
            opponent.indicator = Fencer.indicator - result.fencer_score
            fencer_team_touches_scored = fencer.team.touches_scored + result.fencer_score
            fencer.team.touches_scored = fencer_team_touches_scored
            fencer_team_indicator = fencer.team.indicator + result.fencer_score
            fencer.team.indicator = fencer_team_indicator
            opponent_team_touches_recieved = opponent.team.touches_recieved + result.fencer_score
            opponent.team.touches_recieved = opponent_team_touches_recieved
            opponent_team_indicator = opponent.team.indicator - result.fencer_score
            opponent.team.indicator = opponent_team_indicator
            team_result = Result.query.filter_by(
                pool_id=fencer.team.pool.id,
                team=fencer.team,
                opponent_team=opponent.team).first()
            pool.results.append(result)
            db.session.add(result)
            if team_result is not None:
                team_result.fencer_score = Result.fencer_score + result.fencer_score
            else:
                team_result = Result(
                    pool_id=fencer.team.pool.id,
                    team=fencer.team,
                    opponent_team=opponent.team,
                    fencer_score=result.fencer_score)
                fencer.team.pool.results.append(team_result)
                db.session.add(team_result)
            individual_results = db.session.query(Result).filter(
                Result.team_id == fencer.team.id,
                Result.opponent_team_id == opponent.team.id,
                Result.pool_id != fencer.team.pool.id,
                Result.fencer_win == True).all()
            if len(individual_results) >= 2 and not team_result.fencer_win:
                team_result.fencer_win = True
                fencer.team.victories = Team.victories + 1

        pool.state = 1
        db.session.commit()
        return redirect(url_for('edit_pools', event_id=event_id))
    elif request.method == "GET":
        fencers = pool.fencers.order_by(Fencer.num_in_pool.asc())
        return render_template(
            'edit-pool.html', event=pool.event, pool=pool, fencers=fencers)


@app.route('/event/<int:event_id>/edit-pool-assignment')
@login_required
def edit_pool_assignment(event_id):
    event = Event.query.get_or_404(event_id)
    _pools = event.pools
    pools = {}
    for pool in _pools:
        if pool.pool_letter == 'O':
            pools[pool] = Team.query.filter_by(event_id=event_id, pool_id=pool.id).order_by(Team.num_in_pool).all()
    return render_template('edit-pool-assignment.html', event=event, pools=pools)


@app.route('/event/<int:event_id>/submit-pools', methods=['GET'])
@login_required
def submit_pools(event_id):
    event = Event.query.get_or_404(event_id)
    if not is_to_of_tournament(current_user, event.tournament):
        flash('You do not have permission to access this tournament.')
        return redirect(url_for('index'))
    pools = event.pools
    all_pools_done = False
    if event.is_stage(Stage.POOLS):  # check if all pools done to advance
        all_pools_done = True
        for pool in pools:
            if pool.state == 0 and pool.pool_letter != 'O':
                all_pools_done = False
    if all_pools_done:
        event.advance_stage(Stage.POOL_RESULTS)
    db.session.commit()
    return redirect(url_for('pool_results', event_id=event.id))


@app.route('/event/<int:event_id>/submit-pool-assignment', methods=['POST'])
@login_required
def submit_pool_assignment(event_id):
    event = Event.query.get_or_404(event_id)
    if not is_to_of_tournament(current_user, event.tournament):
        flash('You do not have permission to access this tournament.')
        return redirect(url_for('index'))
    pools = event.pools
    content = request.get_json(silent=True)
    for _pool, teams in content.items():
        for num_in_pool, _team in enumerate(teams):
            team = Team.query.filter_by(name=_team, event=event).first()
            team.pool.num_fencers = Pool.num_fencers - 1
            team.pool.teams.remove(team)
            team.num_in_pool = num_in_pool + 1
            for fencer in team.fencers:
                if fencer.team_position == 'D':
                    continue
                fencer.num_in_pool = num_in_pool + 1
                fencer.pool.num_fencers = Pool.num_fencers - 1
                fencer.pool.fencers.remove(fencer)

                new_pool = Pool.query.filter_by(event_id=event_id, pool_letter=fencer.team_position, poolNum=int(_pool)+1).first()
                new_pool.fencers.append(fencer)
                new_pool.num_fencers = Pool.num_fencers + 1
            pool = Pool.query.filter_by(event_id=event_id, poolNum=int(_pool)+1, pool_letter='O').first()
            team.pool_id = pool.id
            team.pool = pool
            pool.teams.append(team)
            pool.num_fencers = Pool.num_fencers + 1
    event.advance_stage(Stage.POOL_ASSIGNMENTS)
    event.advance_stage(Stage.POOLS)
    db.session.commit()
    return redirect(url_for('edit_pools', event_id=event_id))


@app.route('/event/<int:event_id>/generate-bracket')
@login_required
def generate_bracket(event_id):
    event = Event.query.get_or_404(event_id)
    if not is_to_of_tournament(current_user, event.tournament):
        flash('You do not have permission to access this tournament.')
        return redirect(url_for('index'))
    if event.stage != 8:
        flash('DEs cannot be generated at this stage')
        return redirect(url_for('index'))
    q = db.engine.execute(
        """SELECT t.id, (t.victories*1.0 / (p.num_fencers - 1)) as winPercent
        FROM team t JOIN pool p ON t.Pool = p.id
        WHERE t.is_checked_in = 1 AND t.Event = {}
        ORDER BY winPercent DESC, t.indicator DESC, t.touches_scored DESC LIMIT 12;
        """.format(event_id))
    teams = [Team.query.get(id) for (id, _) in q]
    fencer_names = [(team.name + " (" + str(i+1) + ")") for i, team in enumerate(teams)]
    bracket = generate_tournament(teams)
    num_rounds = int(math.log(len(bracket)*2, 2))
    j = num_rounds
    for i in [2**i for i in range(num_rounds-1)]:
        for _ in range(i):
            de = DE(
                state=4,
                event_id=event.id,
                round=j)
            db.session.add(de)
            event.des.append(de)
        j -= 1
    third = DE(
        state=4,
        is_third=True,
        event_id=event.id,
        round=int(math.log(len(bracket)*2, 2)))
    db.session.add(third)
    event.des.append(de)
    for fencer1, fencer2 in bracket:
        if fencer2 is None:
            de = DE(team1_id=fencer1.id, state=3, round=1)
            event.des.append(de)
            des = de.event.des.filter_by(is_third=False)\
                .order_by(DE.id.asc()).all()
            next_de = des[int(((des.index(de) + 1) & ~(1 << 0))/2) - 1]
            if (des.index(de) + 1) % 2 == 0:
                next_de.team1 = de.team1
            else:
                next_de.team2 = de.team1
            if next_de.team1 is not None and next_de.team2 is not None:
                next_de.state = 0
        else:
            de = DE(
                team1_id=(fencer1.id if fencer1 is not None else None),
                team2_id=(fencer2.id if fencer2 is not None else None),
                state=0, round=1)
            event.des.append(de)
        db.session.add(de)

    tableau = {}
    tableau['teams'] = generate_tournament(fencer_names)
    tableau['results'] = [[] for _ in range(
        int(math.log(len(tableau['teams'])*2, 2)))]
    i = 1
    for round in range(int(math.log(len(tableau['teams'])*2, 2))):
        if round == 0:
            tableau['results'][round] = [[] for _ in range(2)]
            tableau['results'][round][1] = [None, None, 'third']
        else:
            tableau['results'][round] = [[] for _ in range(2 ** round)]
        for match in range(2 ** round):
            tableau['results'][round][match] = [None, None, 'match' + str(i)]
            i += 1
    tableau['results'] = tableau['results'][::-1]
    event.tableau_json = json.dumps(tableau)
    event.advance_stage(Stage.DES)
    db.session.commit()
    return redirect(url_for('edit_DE', event_id=event_id))


@app.route('/de/<int:de_id>/submit', methods=['POST'])
@login_required
def submit_DE(de_id):
    de = DE.query.get(de_id)
    event = Event.query.get_or_404(de.event.id)
    if not is_to_of_tournament(current_user, event.tournament):
        flash('You do not have permission to access this tournament.')
        return redirect(url_for('index'))
    de.fencer1_score = int(request.form['fencer1'])
    de.fencer2_score = int(request.form['fencer2'])
    if de.fencer1_score == de.fencer2_score:
        de.fencer1_win = request.form['fencer1_win']
    else:
        de.fencer1_win = True if de.fencer1_score > de.fencer2_score else False
    de.state = 2
    db.session.commit()
    tableau = json.loads(de.event.tableau_json)

    des = de.event.des.filter_by(is_third=False).order_by(DE.id.asc()).all()

    # not final or semi
    if not de.is_third and des.index(de) not in [0, 1, 2]:
        round = de.round - 1
        match = tableau['results'][round].index(
            [None, None, 'match' + str(des.index(de)+1)])
        tableau['results'][round][match] = [de.fencer1_score, de.fencer2_score]
        de.event.tableau_json = json.dumps(tableau)
        next_de = des[int(((des.index(de) + 1) & ~(1 << 0))/2) - 1]
        if (des.index(de) + 1) % 2 == 0:
            if de.fencer1_win:
                next_de.team1 = de.team1
                de.team2.round_eliminated_in = de.round
            else:
                next_de.team1 = de.team2
                de.team1.round_eliminated_in = de.round
        else:
            if de.fencer1_win:
                next_de.team2 = de.team1
                de.team2.round_eliminated_in = de.round
            else:
                next_de.team2 = de.team2
                de.team1.round_eliminated_in = de.round
        if next_de.team1 is not None and next_de.team2 is not None:
            next_de.state = 0
        # check if all DEs in round finished
        num_eliminated = Team.query.filter_by(
            round_eliminated_in=(round+1), event_id=de.event.id).count()
        byes = DE.query.filter_by(
            event_id=de.event.id, team2_id=None, state=3).count()
        if ((round == 0
                and num_eliminated == (int(len(tableau['teams'])/2**round) - byes))
            or (round > 0
                and num_eliminated == int(len(tableau['teams'])/2**round))):
            q = db.engine.execute(
                """SELECT d.id, abs(d.fencer1_score - d.fencer2_score) AS score
                FROM de d
                WHERE d.event_id = {} AND d.round = {}
                AND d.team2_id IS NOT NULL
                ORDER BY score DESC;""".format(de.event.id, round+1))
            des_in_round = [[DE.query.get(id), j] for (id, j) in q]
            if round == 0:
                place1 = Team.query.filter_by(
                    event_id=de.event.id, is_checked_in=True).count()
            else:
                place1 = int(len(tableau['teams'])/(2**round-1))
            for i, val in enumerate(des_in_round):
                loser_team = val[0].team2 if val[0].fencer1_win else val[0].team1
                loser_team.final_place = place1 - i
                loser_team.de_indicator = val[1]
    elif not de.is_third and des.index(de) in [1, 2]:  # semifinal
        third = event.des.filter_by(is_third=True).first()
        if (des.index(de) + 1) % 2 == 0:
            third.team1 = de.team2 if de.fencer1_win else de.team1
        else:
            third.team2 = de.team2 if de.fencer1_win else de.team1
        if third.team1 is not None and third.team2 is not None:
            third.state = 0
        final = des[0]
        if (des.index(de) + 1) % 2 == 0:
            final.team1 = de.team1 if de.fencer1_win else de.team2
        else:
            final.team2 = de.team1 if de.fencer1_win else de.team2
        if final.team1 is not None and final.team2 is not None:
            final.state = 0
        round = de.round - 1
        match = tableau['results'][round].index(
            [None, None, 'match' + str(des.index(de)+1)])
        tableau['results'][round][match] = [de.fencer1_score, de.fencer2_score]
        de.event.tableau_json = json.dumps(tableau)
    elif de.is_third:  # third
        round = int(math.log(len(tableau['teams']), 2))
        match = tableau['results'][round].index([None, None, 'third'])
        tableau['results'][round][match] = [de.fencer1_score, de.fencer2_score]
        de.event.tableau_json = json.dumps(tableau)
        de.team1.round_eliminated_in = de.round
        de.team2.round_eliminated_in = de.round
        de.team1.final_place = 3 if de.fencer1_win else 4
        de.team2.final_place = 4 if de.fencer1_win else 3
    elif des.index(de) == 0:  # final
        round = int(math.log(len(tableau['teams']), 2))
        match = tableau['results'][round].index([None, None, 'match1'])
        tableau['results'][round][match] = [de.fencer1_score, de.fencer2_score]
        de.event.tableau_json = json.dumps(tableau)
        de.team1.round_eliminated_in = de.round
        de.team2.round_eliminated_in = de.round
        de.team1.final_place = 1 if de.fencer1_win else 2
        de.team2.final_place = 2 if de.fencer1_win else 1
    # check if all DEs finished
    des_not_finished = de.event.des.filter_by(state=0).count()
    if des_not_finished == 0:
        de.event.advance_stage(Stage.EVENT_FINISHED)
    db.session.commit()
    return redirect(url_for('edit_DE', event_id=de.event.id))


@app.route('/event/<int:event_id>/de/edit')
@login_required
def edit_DE(event_id):
    event = Event.query.get_or_404(event_id)
    if not is_to_of_tournament(current_user, event.tournament):
        flash('You do not have permission to access this tournament.')
        return redirect(url_for('index'))
    if event.tableau_json == None:
        return "DEs have not been posted yet."
    des = event.des.order_by(DE.round.asc())
    return render_template(
        'edit-de.html',
        event=event,
        directElims=json.loads(event.tableau_json),
        des=des)


@app.route('/event/<int:event_id>/check-in-team/<int:team_id>')
@login_required
def check_in_team(event_id, team_id):
    event = Event.query.get_or_404(event_id)
    tournament = event.tournament
    if not is_to_of_tournament(current_user, tournament):
        flash('You do not have permission to access this tournament.')
        return redirect(url_for('index'))
    if event.stage >= 4:
        flash('Checking in teams is not allowed after pools have been created.')
        return redirect(url_for('edit_registration', event_id=event_id))
    team = Team.query.get(team_id)
    team.is_checked_in = True
    event.num_fencers_checked_in = Event.num_fencers_checked_in + 1
    db.session.commit()
    return redirect(url_for('edit_registration', event_id=event_id))


@app.route('/event/<int:event_id>/absent-team/<int:team_id>')
@login_required
def make_team_absent(event_id, team_id):
    event = Event.query.get_or_404(event_id)
    tournament = event.tournament
    if not is_to_of_tournament(current_user, tournament):
        flash('You do not have permission to access this tournament.')
        return redirect(url_for('index'))
    if event.stage >= 4:
        flash('Making teams absent is not allowed after pools have been created.')
        return redirect(url_for('edit_registration', event_id=event_id))
    team = Team.query.get(team_id)
    team.is_checked_in = False
    event.num_fencers_checked_in = Event.num_fencers_checked_in - 1
    db.session.commit()
    return redirect(url_for('edit_registration', event_id=event_id))


@app.route(
    '/event/<int:event_id>/edit-team/<int:team_id>', methods=['POST', 'GET'])
@login_required
def edit_team(event_id, team_id):
    event = Event.query.get_or_404(event_id)
    tournament = event.tournament
    if not is_to_of_tournament(current_user, tournament):
        flash('You do not have permission to access this tournament.')
        return redirect(url_for('index'))
    team = Team.query.get(team_id)
    old_club = team.club
    fencer_a = Fencer.query.filter_by(team_id=team.id, team_position='A').first()
    fencer_b = Fencer.query.filter_by(team_id=team.id, team_position='B').first()
    fencer_c = Fencer.query.filter_by(team_id=team.id, team_position='C').first()
    fencer_d = Fencer.query.filter_by(team_id=team.id, team_position='D').first()
    form = AddTeamForm()
    if form.validate_on_submit():
        team.name = form.teamName.data
        if form.fencer_a.data != fencer_a.first_name + ' ' + fencer_a.last_name:
            fencer_a.first_name = form.fencer_a.data.split()[0].title()
            fencer_a.last_name = form.fencer_a.data.split()[1].title()
        if form.fencer_b.data != fencer_b.first_name + ' ' + fencer_b.last_name:
            fencer_b.first_name = form.fencer_b.data.split()[0].title()
            fencer_b.last_name = form.fencer_b.data.split()[1].title()
        if form.fencer_c.data != fencer_c.first_name + ' ' + fencer_c.last_name:
            name = form.fencer_c.data
            if name != '':
                fencer_c.first_name = form.fencer_c.data.split()[0].title()
                fencer_c.last_name = form.fencer_c.data.split()[1].title()
            else:
                fencer_c.first_name = ''
                fencer_c.last_name = ''
        if form.fencer_d.data != fencer_d.first_name + ' ' + fencer_d.last_name:
            name = form.fencer_d.data
            if name != '':
                fencer_d.first_name = form.fencer_d.data.split()[0].title()
                fencer_d.last_name = form.fencer_d.data.split()[1].title()
            else:
                fencer_d.first_name = ''
                fencer_d.last_name = ''
        if form.club.data != team.club.name:
            old_club.teams.remove(team)
            club = Club.query.filter_by(name=form.club.data).first()
            if club is None:
                club = Club(name=form.club.data)
                db.session.add(club)
            else:
                club.teams.append(team)
            team.club = club
        flash('Edited team')
        db.session.commit()
        return redirect(url_for('edit_registration', event_id=event_id))
    elif request.method == 'GET':
        form.submit.label.text = "Edit Team"
        form.teamName.data = team.name
        form.fencer_a.data = fencer_a.first_name + ' ' + fencer_a.last_name
        form.fencer_b.data = fencer_b.first_name + ' ' + fencer_b.last_name
        form.fencer_c.data = fencer_c.first_name + ' ' + fencer_c.last_name
        form.fencer_d.data = fencer_d.first_name + ' ' + fencer_d.last_name
        form.club.data = Club.query.get(team.club_id).name
        return render_template('edit-team.html', form=form)


@app.route('/event/<int:event_id>/delete-team/<int:team_id>')
@login_required
def delete_team(event_id, team_id):
    event = Event.query.get_or_404(event_id)
    if not is_to_of_tournament(current_user, event.tournament):
        flash('You do not have permission to access this tournament.')
        return redirect(url_for('index'))
    if event.stage >= 3:
        flash('Deleting teams is not allowed after pools have been created.')
        return redirect(url_for('edit_registration', event_id=event_id))
    team = Team.query.get_or_404(team_id)
    if team.is_checked_in:
        event.num_fencers_checked_in = Event.num_fencers_checked_in - 1
    event.num_fencers = Event.num_fencers - 1
    db.session.delete(team)
    for fencer in team.fencers:
        db.session.delete(fencer)
    db.session.commit()
    return redirect(url_for('edit_registration', event_id=event_id))


@app.route('/event/<int:event_id>/open-registration')
@login_required
def open_registration(event_id):
    event = Event.query.get_or_404(event_id)
    tournament = event.tournament
    if not is_to_of_tournament(current_user, tournament):
        flash('You do not have permission to access this tournament.')
        return redirect(url_for('index'))
    event.advance_stage(Stage.REGISTRATION_OPEN)
    db.session.commit()
    return redirect(url_for('edit_registration', event_id=event_id))


@app.route('/event/<int:event_id>/close-registration')
@login_required
def close_registration(event_id):
    event = Event.query.get_or_404(event_id)
    tournament = event.tournament
    if not is_to_of_tournament(current_user, tournament):
        flash('You do not have permission to access this tournament.')
        return redirect(url_for('index'))
    event.advance_stage(Stage.REGISTRATION_CLOSED)
    db.session.commit()
    return redirect(url_for('edit_registration', event_id=event_id))


@app.route('/event/<int:event_id>/create-pools', methods=['GET', 'POST'])
@login_required
def create_pools(event_id):
    event = Event.query.get_or_404(event_id)
    if not is_to_of_tournament(current_user, event.tournament):
        flash('You do not have permission to access this tournament.')
        return redirect(url_for('index'))
    form = CreatePoolForm()
    teams = event.teams.filter_by(is_checked_in=True)\
        .order_by(Team.club_id.asc(), func.random())
    form.num_fencers.data = event.num_fencers_checked_in
    if form.validate_on_submit():

        def add_pools(event, pool_num, num_fencers):
            letters = ['A', 'B', 'C', 'O']
            tmp_pools = []
            for letter in letters:
                pool = Pool(event_id=event.id, num_fencers=num_fencers,
                            poolNum=pool_num, pool_letter=letter)
                tmp_pools.append(pool)
                db.session.add(pool)
            return tmp_pools

        pools = []
        pool_num = 1
        for _ in range(0, form.numPools1.data):
            pools.append(add_pools(event, pool_num, form.numFencers1.data))
            pool_num += 1
        for _ in range(0, form.numPools2.data):
            pools.append(add_pools(event, pool_num, form.numFencers2.data))
            pool_num += 1

        pools = quicksort(pools)

        pool_num = [0 for _ in pools]
        for i, team in enumerate(teams):
            pools[i % len(pools)][3].teams.append(team)
            team.num_in_pool = pool_num[i % len(pools)] + 1
            team.pool = pools[i % len(pools)][3]
            fencers = team.fencers.order_by(Fencer.team_position.asc())
            for j in range(3):
                pools[i % len(pools)][j].fencers.append(fencers[j])
                fencers[j].num_in_pool = pool_num[i % len(pools)] + 1
                fencers[j].pool = pools[i % len(pools)][j]
            pool_num[i % len(pools)] += 1

        event.advance_stage(Stage.INITIAL_SEEDING)
        db.session.commit()
        return redirect(url_for('initial_seeding', event_id=event_id))
    return render_template(
        'create-pools.html',
        form=form,
        event=event,
        num_fencers=event.num_fencers_checked_in)


@app.route('/reset-password-request', methods=['GET', 'POST'])
def reset_password_request():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    form = ResetPasswordRequestForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data).first()
        if user:
            send_password_reset_email(user)
        flash('Check your email for the instructions to reset your password')
        return redirect(url_for('login'))
    return render_template(
        'reset-password-request.html',
        title='Reset Password',
        form=form)


@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    user = User.verify_reset_password_token(token)
    if not user:
        return redirect(url_for('index'))
    form = ResetPasswordForm()
    if form.validate_on_submit():
        user.set_password(form.password.data)
        db.session.commit()
        flash('Your password has been reset.')
        return redirect(url_for('login'))
    return render_template('reset-password.html', form=form)


@app.route('/preregister/<token>', methods=['GET', 'POST'])
def preregister(token):
    json = jwt.decode(token, app.config['SECRET_KEY'],
                    algorithms=['HS256'])
    if not json:
        return redirect(url_for('index'))
    club = Club.query.get(json['club'])
    tournament = Tournament.query.get(json['tournament'])
    foil_event = tournament.events.filter_by(weapon='foil').first()
    epee_event = tournament.events.filter_by(weapon='epee').first()
    saber_event = tournament.events.filter_by(weapon='saber').first()
    form = PreregisterForm()
    if form.validate_on_submit():

        def add_team(club, event, team_name, name_list):
            letters = ['A', 'B', 'C', 'D']
            team = Team(name=team_name, is_checked_in=False)
            event.teams.append(team)
            club.teams.append(team)
            event.num_fencers += 1
            for i, name in enumerate(name_list):
                if name is None or name == '':
                    name = ['', '']
                else:
                    name = name.split()
                fencer = Fencer(first_name=name[0], last_name=name[1], team_position=letters[i])
                team.fencers.append(fencer)
                db.session.add(fencer)

        if request.form.get('Foil_A_slider'):
            team_name = club.name + " Foil "
            if request.form.get('Foil_B_slider'):
                team_name += 'A'
            add_team(club, foil_event, team_name, [
                form.Foil_A.fencer_a.data,
                form.Foil_A.fencer_b.data,
                form.Foil_A.fencer_c.data,
                form.Foil_A.fencer_d.data])
        if request.form.get('Foil_B_slider'):
            team_name = club.name + " Foil "
            if request.form.get('Foil_A_slider'):
                team_name += 'B'
            add_team(club, foil_event, team_name, [
                form.Foil_B.fencer_a.data,
                form.Foil_B.fencer_b.data,
                form.Foil_B.fencer_c.data,
                form.Foil_B.fencer_d.data])
        if request.form.get('Epee_A_slider'):
            team_name = club.name + " Epee "
            if request.form.get('Epee_B_slider'):
                team_name += 'A'
            add_team(club, epee_event, team_name, [
                form.Epee_A.fencer_a.data,
                form.Epee_A.fencer_b.data,
                form.Epee_A.fencer_c.data,
                form.Epee_A.fencer_d.data])
        if request.form.get('Epee_B_slider'):
            team_name = club.name + " Epee "
            if request.form.get('Epee_A_slider'):
                team_name += 'B'
            add_team(club, epee_event, team_name, [
                form.Epee_B.fencer_a.data,
                form.Epee_B.fencer_b.data,
                form.Epee_B.fencer_c.data,
                form.Epee_B.fencer_d.data])
        if request.form.get('Saber_A_slider'):
            team_name = club.name + " Saber "
            if request.form.get('Saber_B_slider'):
                team_name += 'A'
            add_team(club, saber_event, team_name, [
                form.Saber_A.fencer_a.data,
                form.Saber_A.fencer_b.data,
                form.Saber_A.fencer_c.data,
                form.Saber_A.fencer_d.data])
        if request.form.get('Saber_B_slider'):
            team_name = club.name + " Saber "
            if request.form.get('Saber_A_slider'):
                team_name += 'B'
            add_team(club, saber_event, team_name, [
                form.Saber_B.fencer_a.data,
                form.Saber_B.fencer_b.data,
                form.Saber_B.fencer_c.data,
                form.Saber_B.fencer_d.data])
        db.session.commit()
        flash('You have preregistered.')
        return redirect(url_for('index'))
    return render_template('preregistration.html', title='Preregistration', form=form, tournament=tournament, club=club)


@app.route('/tournament/<int:tournament_id>/send-prereg-email', methods=['GET', 'POST'])
def send_prereg(tournament_id):
    tournament = Tournament.query.get_or_404(tournament_id)
    if not is_to_of_tournament(current_user, tournament):
        flash('You do not have permission to access this tournament.')  # TODO: add this to similar checks
        return redirect(url_for('index'))
    form = EmailListForm()
    if form.validate_on_submit():
        try:
            data = json.load(form.email_json.data.stream)
        except JSONDecodeError as e:
            flask('Invalid JSON')
            return redirect(url_for('send_prereg', tournament_id=tournament.id))
        for club_name, email in data.items():
            if club_name not in app.config['UNIVERSITIES'] or not re.match(r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$", email):
                flash('Invalid JSON')
                return redirect(url_for('send_prereg', tournament_id=tournament.id))
            club = Club.query.filter_by(name=club_name).first()
            if club is None:
                club = Club(name=club_name)
                db.session.add(club)
                db.session.commit()
            token = jwt.encode(
                {'club': club.id, 'tournament': tournament.id, 'exp': time() + 604800},
                app.config['SECRET_KEY'], algorithm='HS256').decode('utf-8')
            send_prereg_email(email, token, club, tournament)
        flash('Preregistration emails have been sent.')
        return redirect(url_for('edit_tournament', tournament_id=tournament.id))
    return render_template('send-prereg-email.html', title='Preregistration', form=form, tournament=tournament)


@app.route('/event/<int:event_id>/de-sheet/<int:team1_id>/<int:team2_id>')
def de_sheet(event_id, team1_id, team2_id):
    team1 = Team.query.get_or_404(team1_id)
    team2 = Team.query.get_or_404(team2_id)
    return render_template('de-sheet.html', team1=team1, team2=team2)


@app.route('/event/<int:event_id>/pool/<int:pool_id>/delete-pool')
@login_required
def delete_pool(event_id, pool_id):
    event = Event.query.get_or_404(event_id)
    if not is_to_of_tournament(current_user, event.tournament):
        flash(' You do not have permission to access this tournament.')
        return redirect(url_for('index'))
    db.session.query(Result).filter(Result.pool_id == pool_id).delete(False)
    pool = Pool.query.get_or_404(pool_id)
    pool.state = 0
    db.session.commit()
    return redirect(url_for('edit_pools', event_id=event_id))


#flask_profiler.init_app(app)
