'''
All logic for generating and retriving content recommendations.
Three main functions:
    - get_resume_recommendations(user)
    - get_next_recommendations(user)
    - get_explore_recommendations(user)
'''
import datetime
import random
import copy

from django.db.models import Count

from kalite.topic_tools import *
from kalite.facility.models import FacilityUser
from kalite.main.models import ExerciseLog , VideoLog, ContentLog
from fle_utils.general import softload_json, json_ascii_decoder



#some things to cache (exercise parents_lookup table isn't as intense, but is used frequently)
topic_tree = None
exercise_parents_lookup_table = None
CACHE_VARS.append("topic_tree")
CACHE_VARS.append("exercise_parents_lookup_table")

TOPICS_FILEPATHS = {
    settings.CHANNEL: os.path.join(settings.CHANNEL_DATA_PATH, "topics.json")
}
########################################## 'RESUME' LOGIC #################################################

###
# Returns a list of all started but NOT completed exercises
#
# @param user: facility user model
# @return: The most recent video/exercise that has been started but NOT completed
###
def get_resume_recommendations(user):
    final = get_most_recent_incomplete_item(user)
    if final:
        return [final] #for first pass, just return the most recent video!!!
    else:
        return []


####################################### 'NEXT STEPS' LOGIC ################################################

###
# Returns a list of exercises to go to next. Influenced by other user patterns in the same group as well
# as the user's struggling pattern shown in the exercise log.
#
# Full List:
# - Incomplete exercises (where user left off - MOVED TO RESUME)
# - Struggling (pre-reqs for exercises marked as "struggling" for the student)
# - User patterns based on group analysis (maximum likelihood estimation and empirical count)
# - Topic tree structure recommendations based on the most recent subtopic accessed
#
# @param user: facility user model
#        
# @return: a list of exercise id's and their titles, [type], and subtopic for where the user 
#          should consider going next.
###
def get_next_recommendations(user):

    #user = ExerciseLog.objects.filter(id__lte=1)[4].user        #random person, can delete after    

    exercise_parents_table = get_exercise_parents_lookup_table()
    topic_table = get_topic_tree_lookup_table()

    most_recent = get_most_recent_exercises(user)

    if len(most_recent) > 0 and most_recent[0] in exercise_parents_table:
        current_subtopic = exercise_parents_table[most_recent[0]]['subtopic_id']
    else:
        current_subtopic = None

    #logic for recommendations based off of the topic tree structure
    if current_subtopic:
        topic_tree_based_data = generate_recommendation_data()[current_subtopic]['related_subtopics'][:3]
        topic_tree_based_data = get_exercises_from_topics(topic_tree_based_data)
    else:
        topic_tree_based_data = []
    
    #for checking that only exercises that have not been accessed are returned
    check = [] 
    for ex in topic_tree_based_data:
        if not ex in most_recent:
            check.append(ex)

    topic_tree_based_data = check

    #logic to generate recommendations based on exercises student is struggling with
    struggling = get_exercise_prereqs(get_struggling_exercises(user))   

    #logic to get recommendations based on group patterns, if applicable
    group = get_group_recommendations(user)

   
    #now append titles and other metadata to each exercise id
    final = [] # final data to return
    for exercise_id in (group[:2] + struggling[:2] + topic_tree_based_data[:1]):  #notice the concatenation

        if exercise_id in exercise_parents_table:
            subtopic_id = exercise_parents_table[exercise_id]['subtopic_id']
            exercise = get_exercise_cache()[exercise_id]
            exercise["topic"] = topic_table[subtopic_id]
            final.append(exercise)


    #final recommendations are a combination of struggling, group filtering, and topic_tree filtering
    return final




# Given a facility user model, return a list of ALL exercises (ids) that are immediately tackled by other users
# in the same user group - also ordered by empirical count (more people moving onto this -> higher in the
# list). "Immediately" means the very next exercise after the most recent one the given user has accessed.
#
# This function checks if the exercises returned have been accessed already, and only returns those that
# have not been.
#
# A group is defined as a collection of students within the same facility and group (as defined in models)
def get_group_recommendations(user):
 
    recent_exercises = get_most_recent_exercises(user)
    
    user_list = FacilityUser.objects.filter(group=user.group)

    if recent_exercises:

        #If the user has recently engaged with exercises, use these exercises as the basis for filtering
        user_exercises = ExerciseLog.objects.filter(user__in=user_list).order_by("-latest_activity_timestamp").extra(select={'null_complete': "completion_timestamp is null"}, order_by=["-null_complete", "-completion_timestamp"])
    
        exercise_counts = {}

        for user in user_list:
            user_logs = user_exercises.filter(user=user)
            for i, log in enumerate(user_logs):
                if i > 0:
                    prev_log = user_logs[i-1]
                    if log.exercise_id in recent_exercises:
                        if prev_log.exercise_id in exercise_counts:
                            exercise_counts[prev_log.exercise_id] += 1
                        else:
                            exercise_counts[prev_log.exercise_id] = 1

        exercise_counts = [{"exercise_id": key, "count": value} for key, value in exercise_counts.iteritems()]

    else:
        #If not, only look at the group data
        exercise_counts = ExerciseLog.objects.filter(user__in=user_list).values("exercise_id").annotate(count=Count("exercise_id"))

    #sort the results in order of highest counts to smallest
    sorted_counts = sorted(exercise_counts, key=lambda k:k['count'], reverse=False)
    
    group_rec = []  #the final list of recommendations to return, WITHOUT counts

    for c in sorted_counts:
        group_rec.append(c['exercise_id'])

    return group_rec




# Given a facility user model, return a list ALL exercises (ids) that the user is struggling on
# This amounts to returning only those exercises that have their "struggling" attribute set
# to True. The exercise ids are also in order of most recent first. 
def get_struggling_exercises(user):
    
    exercises_by_user = ExerciseLog.objects.filter(user=user)

    #sort exercises first, in order of most recent first
    exercises_by_user = sorted(exercises_by_user, key=lambda student: student.completion_timestamp, reverse=True)

    struggles = []                                              #TheStruggleIsReal
    for exercise in exercises_by_user:
        if exercise.struggling:
            struggles.append(exercise.exercise_id)

    return struggles




# Given a list of exercise ids, return a concatenated list of prereqs for each of the exercises
def get_exercise_prereqs(exercises):
    ex_cache = get_exercise_cache()
    prereqs = []
    for exercise in exercises:
        prereqs += ex_cache[exercise]['prerequisites']

    return prereqs
    



########################################## 'EXPLORE' LOGIC ################################################

###
# Returns a list of subtopic ids that the user has not explored yet. 
#
# @param: user - the facility user model corresponding to the current user
#
# @return: a list of exercise id's of the 'middle to farthest neighbors,' or less immediately relevant
#           exercises based on topic tree structure.
###
def get_explore_recommendations(user):

    ''' 
        Logic: grab 3 random exercises from recent exercises, get their subtopic ids, then using
        generate_recommendation_data(), get the elements at certain positions (nearest). 

    '''

    #user = ExerciseLog.objects.filter(id__lte=1)[4].user            #random person, can delete after   

    data = generate_recommendation_data()                           #topic tree alg
    exercise_parents_table = get_exercise_parents_lookup_table()    #for finding out subtopic ids
    recent_exercises = get_most_recent_exercises(user)              #most recent ex

    #simply getting a list of subtopics accessed by user
    recent_subtopics = []
    for ex in recent_exercises:
        if not exercise_parents_table[ex]['subtopic_id'] in recent_subtopics:
            recent_subtopics.append(exercise_parents_table[ex]['subtopic_id'])

    #choose sample number, up to three
    if len(recent_exercises) > 0:
        sampleNum = 1 #must be at least 1

        if len(recent_exercises) > 1:
            sumpleNum = 2

            if len(recent_exercises) > 2:
                sampleNum = 3

    else:
        sampleNum = 0 #user has not attempted any
    
    random_exercises = random.sample(recent_exercises, sampleNum)   #grab the valid/appropriate number of exs, up to 3
    added = []                                                      #keep track of what has been added (below)
    final = []                                                      #final recommendations to return
    
    for ex in random_exercises:
        exercise_data = exercise_parents_table[ex]
        subtopic_id = exercise_data['subtopic_id']                      #subtopic_id of current
        related_subtopics = data[subtopic_id]['related_subtopics'][2:7] #get recommendations based on this, can tweak numbers!

        recommended_topics = []                                         #the recommended topics
    
        for subtopic in related_subtopics:
            curr = get_subtopic_data(subtopic)

            if not curr['id'] in recent_subtopics:                      #check for an unaccessed recommendation
                recommended_topics.append(curr)                         #add to return
                                                                                             
        to_append = []
        if len(recommended_topics) > 0:#if recommendation present
            suggested = recommended_topics[0]           #the suggested topic + its data
            accessed  = exercise_data['subtopic_title'] #corresponds to interest_topic in view

            to_append = {

                'suggested_topic': {
                    'title':suggested['title'], 'path': suggested['path'],
                    'description': suggested['description']
                },

                'interest_topic':{'title': accessed}
            }
    
        #if valid (i.e. not a repeat and also some recommendations)
        if (not exercise_data['subtopic_id'] in added):   
            final.append(to_append)                                     #valid, so append
            added.append(exercise_data['subtopic_id'])                  #make note


    return final




#given a subtopic id, return corresponding data to return in get_explore_recommendations
def get_subtopic_data(subtopic_id):

    ### topic tree for traversal###
    tree = get_topic_tree_lookup_table()

    if subtopic_id in tree:
        return {
            'id':subtopic_id,
            'title': tree[subtopic_id]['title'],
            'path': tree[subtopic_id]['path'],
            'description':tree[subtopic_id]['description']
        }

   
    return [] #ideally should never get here



##################################### GENERAL HELPER FUNCTIONS ############################################


#returns a dictionary of exercises with their metadata.
#subtopics are the immediate parents (ex: early-math, biology) and topics are one more level above (math)
def get_exercise_parents_lookup_table():
    global exercise_parents_lookup_table

    if exercise_parents_lookup_table:
        return exercise_parents_lookup_table

    ### topic tree for traversal###
    tree = generate_topic_tree()

    #create a lookup table from traversing the tree - can cache if needed, but is decently fast if TOPICS exists
    exercise_parents_table = {}

    #3 possible layers
    for topic in tree['children']:
        for subtopic in topic['children']:
            for ex in subtopic['children']:

                exercise_parents_table[ ex['id'] ] = { "subtopic_id":subtopic['id'], "topic_id":topic['id'],
                    "subtopic_title":subtopic['title'], "topic_title": topic['title'] , "kind":ex['kind'], "title":ex['title'],
                    "description": ex['description']}

                if 'children' in ex: #if there is another layer of children

                    for ex2 in ex['children']:

                        exercise_parents_table[ ex2['id'] ] = { "subtopic_id":subtopic['id'], "topic_id":topic['id'],
                        "subtopic_title":subtopic['title'], "topic_title": topic['title'] , "kind":ex2['kind'],"title":ex['title'],
                        "description": ex['description']}

                        #if there is yet another level
                        if 'children' in ex2:

                            for ex3 in ex2['children']:
                    
                                exercise_parents_table[ ex3['id'] ] = { "subtopic_id":subtopic['id'], "topic_id":topic['id'],
                                "subtopic_title":subtopic['title'], "topic_title": topic['title'] , "kind":ex3['kind'],"title":ex['title'],
                                "description": ex['description']}

                  
    return exercise_parents_table


#Given a list of subtopic/topic ids, returns an ordered list of the first 5 exercise ids under those ids
def get_exercises_from_topics(topicId_list):
    exs = []
    for topic in topicId_list:

        exercises = get_topic_exercises(topic)[:5] #can change this line to allow for more to be returned
        for e in exercises:
            exs += [e['id']] #only add the id to the list

    return exs


#given a facility user model, returns information of the
#most recently accessed and incomplete video/exercise. Can expand this later on to
#include more later, like all items in order or perhaps more logs to look at. 
def get_most_recent_incomplete_item(user):
    #get the queryset objects
    exercise_list = list(ExerciseLog.objects.filter(user=user, complete=False).order_by("-latest_activity_timestamp")[:1])
    video_list = list(VideoLog.objects.filter(user=user, complete=False).order_by("-latest_activity_timestamp")[:1])
    content_list = list(ContentLog.objects.filter(user=user, complete=False).order_by("-latest_activity_timestamp")[:1])

    item_list = []

    if exercise_list:
        item_list.append({
            "timestamp": exercise_list[0].latest_activity_timestamp or datetime.datetime.min,
            "id": exercise_list[0].exercise_id,
            "kind": "Exercise",
        })
    if video_list:
        item_list.append({
            "timestamp": video_list[0].latest_activity_timestamp or datetime.datetime.min,
            "id": video_list[0].video_id,
            "kind": "Content",
        })
    if content_list:
        item_list.append({
            "timestamp": content_list[0].latest_activity_timestamp or datetime.datetime.min,
            "id": content_list[0].content_id,
            "kind": "Content",
        })

    if item_list:
        item_list.sort(key=lambda x: x["timestamp"])
        item = item_list[0]
        if item.get("kind") == "Content":
            return get_content_cache().get(item.get("id"))
        if item.get("kind") == "Exercise":
            return get_exercise_cache().get(item.get("id"))
    else:
        return None

#given a facility user model, return the most recent exercise ids - incomplete AND complete
def get_most_recent_exercises(user):
    exercises_by_user = ExerciseLog.objects.filter(user=user).order_by("-latest_activity_timestamp")

    final = [log.exercise_id for log in exercises_by_user]
    
    return final


#returns a topic tree representation like in the older versions of ka-lite
#import time
def generate_topic_tree(channel=settings.CHANNEL, language=settings.LANGUAGE_CODE):
    #start = time.clock()
    global topic_tree

    #cached
    if topic_tree:
        return topic_tree

    #fun recursion?
    #lookup_table = get_topic_tree_lookup_table(tree)
    #topic_tree = recursively_append_children(topic_tree, lookup_table)

    #hehe
    TOPICS = {}
    TOPICS[channel] = {}
    TOPICS[channel][language] = softload_json(TOPICS_FILEPATHS.get(channel), logger=logging.debug, raises=False)    
    topic_tree = TOPICS[settings.CHANNEL][settings.LANGUAGE_CODE]

    return topic_tree


#helper function for generate_topic_tree() - returns a lookup table that stores object ids and their
#associated meta data needed for generate_topic_tree()
def get_topic_tree_lookup_table(tree=get_topic_tree()):
    table = {}
    for item in tree:
        curr = {
            'id' : item['id'],
            'title' : item['title'],
            'kind' : item['kind'],
            'path' : item['path'],
            'description' : item['description'],
            'parent': item['parent']
        }

        #if current item is NOT a video or exercise, also include the children
        if not (item['kind'] == 'Video' or item['kind'] == 'Exercise'):
            curr['children'] = item['children']

        table[ item['id'] ] = curr

    return table


###################################### BEGIN NEAREST NEIGHBORS ############################################

### MULTI-PURPOSE NEAREST NEIGHBORS ALGORITH, USE AS YOU PLEASE ###
### THE MAIN THING TO REMEMBER IS THAT get_recommended_exercises(subtopic) IS THE MAIN FUNCTION TO CALL ###

###
# Returns a dictionary with each subtopic and their related
# topics.
#
###
def generate_recommendation_data():

    #hardcoded data, each subtopic is the key with its related subtopics and current courses as the values. Not currently in use.
    data_hardcoded = {
        "early-math": {"related_subtopics": ["early-math", "arithmetic", "recreational-math"], "unrelated_subtopics": ["music", "history", "biology"]},
        "arithmetic": {"related_subtopics": ["arithmetic", "pre-algebra", "recreational-math"], "unrelated_subtopics": ["music", "history", "biology"]},
        "pre-algebra": {"related_subtopics": ["pre-algebra", "algebra", "recreational-math"], "unrelated_subtopics": ["music", "history", "biology"]},
        "algebra": {"related_subtopics": ["algebra", "geometry", "recreational-math", "competition-math", "chemistry"], "unrelated_subtopics": ["music", "history", "biology", "cosmology-and-astronomy"]},
        "geometry": {"related_subtopics": ["geometry", "algebra2", "recreational-math", "competition-math", "chemistry"], "unrelated_subtopics": ["music", "history", "biology", "cosmology-and-astronomy"]},
        "algebra2": {"related_subtopics": ["algebra2", "trigonometry", "probability", "competition-math", "chemistry", "microeconomics", "macroeconomics"], "unrelated_subtopics": ["music", "history", "biology", "cosmology-and-astronomy", "lebron-asks-subject", "art-history", "CAS-biodiversity", "Exploratorium"]},
        "trigonometry": {"related_subtopics": ["trigonometry", "linear-algebra", "precalculus", "physics", "microeconomics", "macroeconomics"], "unrelated_subtopics": ["music", "history", "biology", "cosmology-and-astronomy", "lebron-asks-subject", "art-history", "CAS-biodiversity", "Exploratorium"]},
        "probability": {"related_subtopics": ["probability", "recreational-math"], "unrelated_subtopics": ["music", "history", "biology", "cosmology-and-astronomy", "lebron-asks-subject", "art-history", "CAS-biodiversity", "Exploratorium"]},
        "precalculus": {"related_subtopics": ["precalculus", "differential calculus", "probability"], "unrelated_subtopics": ["music", "history", "biology", "cosmology-and-astronomy", "lebron-asks-subject", "art-history", "CAS-biodiversity", "Exploratorium"]},
        "differential-calculus": {"related_subtopics": ["differential-calculus", "differential-equations", "physics", "microeconomics", "macroeconomics"], "unrelated_subtopics": ["music", "history", "biology", "cosmology-and-astronomy", "lebron-asks-subject", "art-history", "CAS-biodiversity", "Exploratorium"]},
        "integral-calculus": {"related_subtopics": ["integral-calculus", "differential-equations", "physics", "microeconomics", "macroeconomics"], "unrelated_subtopics": ["music", "history", "biology", "cosmology-and-astronomy", "lebron-asks-subject", "art-history", "CAS-biodiversity", "Exploratorium"]},
        "multivariate-calculus": {"related_subtopics": ["multivariate-calculus", "differential-equations", "physics", "microeconomics", "macroeconomics"], "unrelated_subtopics": ["music", "history", "biology", "cosmology-and-astronomy", "lebron-asks-subject", "art-history", "CAS-biodiversity", "Exploratorium"]},
        "differential-equations": {"related_subtopics": ["differential-equations", "physics", "microeconomics", "macroeconomics"], "unrelated_subtopics": ["music", "history", "biology", "cosmology-and-astronomy", "lebron-asks-subject", "art-history", "CAS-biodiversity", "Exploratorium", "discoveries-projects"]},
        "linear-algebra": {"related_subtopics": ["linear-algebra", "precalculus"], "unrelated_subtopics": ["music", "history", "biology", "cosmology-and-astronomy", "lebron-asks-subject", "art-history", "CAS-biodiversity", "Exploratorium", "discoveries-projects"]},
        "recreational-math": {"related_subtopics": ["recreational-math", "pre-algebra", "algebra", "geometry", "algebra2"], "unrelated_subtopics": ["music", "history", "biology", "cosmology-and-astronomy", "lebron-asks-subject", "art-history", "CAS-biodiversity", "Exploratorium", "discoveries-projects"]},
        "competition-math": {"related_subtopics": ["competition-math","algebra", "geometry", "algebra2"], "unrelated_subtopics": ["music", "history", "biology", "cosmology-and-astronomy", "lebron-asks-subject", "art-history", "CAS-biodiversity", "Exploratorium", "discoveries-projects"]},


        "biology": {"related_subtopics": ["biology", "health-and-medicine", "CAS-biodiversity", "Exploratorium", "chemistry", "physics", "cosmology-and-astronomy", "nasa"], "unrelated_subtopics": ["music", "philosophy", "microeconomics", "macroeconomics", "history", "art-history", "asian-art-museum"]},
        "physics": {"related_subtopics": ["physics", "discoveries-projects", "cosmology-and-astronomy", "nasa", "Exploratorium", "biology", "CAS-biodiversity", "health-and-medicine", "differential-calculus"], "unrelated_subtopics": ["music", "philosophy", "microeconomics", "macroeconomics", "history", "art-history", "asian-art-museum"]},
        "chemistry": {"related_subtopics": ["chemistry", "organic-chemistry", "biology", "health-and-medicine", "physics", "cosmology-and-astronomy", "discoveries-projects", "CAS-biodiversity", "Exploratorium", "nasa"], "unrelated_subtopics": ["music", "philosophy", "microeconomics", "macroeconomics", "history", "art-history", "asian-art-museum"]},
        "organic-chemistry": {"related_subtopics": ["organic-chemistry", "biology", "health-and-medicine", "physics", "cosmology-and-astronomy", "discoveries-projects", "CAS-biodiversity", "Exploratorium", "nasa"], "unrelated_subtopics": ["music", "philosophy", "microeconomics", "macroeconomics", "history", "art-history", "asian-art-museum"]},
        "cosmology-and-astronomy": {"related_subtopics": ["cosmology-and-astronomy", "nasa", "chemistry", "biology", "health-and-medicine", "physics", "discoveries-projects", "CAS-biodiversity", "Exploratorium", "nasa"], "unrelated_subtopics": ["music", "philosophy", "microeconomics", "macroeconomics", "history", "art-history", "asian-art-museum"]},
        "health-and-medicine": {"related_subtopics": ["health-and-medicine", "biology", "chemistry", "CAS-biodiversity", "Exploratorium", "physics", "cosmology-and-astronomy", "nasa"], "unrelated_subtopics": ["music", "philosophy", "microeconomics", "macroeconomics", "history", "art-history", "asian-art-museum"]},
        "discoveries-projects": {"related_subtopics": ["discoveries-projects", "physics", "computing", "cosmology-and-astronomy", "nasa", "Exploratorium", "biology", "CAS-biodiversity", "health-and-medicine", "differential-calculus"], "unrelated_subtopics": ["music", "philosophy", "microeconomics", "macroeconomics", "history", "art-history", "asian-art-museum"]},

        "microeconomics": {"related_subtopics": ["microeconomics", "macroeconomics"], "unrelated_subtopics": ["music", "philosophy", "microeconomics", "macroeconomics", "history", "art-history", "asian-art-museum"]},
        "macroeconomics": {"related_subtopics": ["macroeconomics", "microeconomics", "core-finance"], "unrelated_subtopics": ["music", "philosophy", "microeconomics", "macroeconomics", "history", "art-history", "asian-art-museum"]},
        "core-finance": {"related_subtopics": ["core-finance", "entrepreneurship2", "macroeconomics", "microeconomics", "core-finance"], "unrelated_subtopics": ["music", "philosophy", "microeconomics", "macroeconomics", "history", "art-history", "asian-art-museum"]},
        "entrepreneurship2": {"related_subtopics": ["entrepreneurship2", "core-finance", "macroeconomics", "microeconomics", "core-finance"], "unrelated_subtopics": ["music", "philosophy", "microeconomics", "macroeconomics", "history", "art-history", "asian-art-museum"]},

        "history": {"related_subtopics": ["history", "art-history", "american-civics-subject", "asian-art-museum", "Exploratorium"], "unrelated_subtopics": ["biology", "music", "health-and-medicine"]},
        "art-history": {"related_subtopics": ["art-history", "ap-art-history", "asian-art-museum", "history", "american-civics-subject", "Exploratorium"], "unrelated_subtopics": ["biology", "music", "health-and-medicine"]},
        "american-civics-subject": {"related_subtopics": ["american-civics-subject", "history"], "unrelated_subtopics": ["biology", "music", "health-and-medicine"]},
        "music": {"related_subtopics": ["music"], "unrelated_subtopics": ["biology", "health-and-medicine"]},
        "philosophy": {"related_subtopics": ["philosophy"]},

        "computing": {"related_subtopics": ["computing", "early-math", "arithmetic", "pre-algebra", "geometry", "probability", "recreational-math", "biology", "physics", "chemistry", "organic-chemistry", "health-and-medicine", "discoveries-projects", "microeconomics", "macroeconomics", "core-finance", "music"]},

        "sat": {"related_subtopics": ["sat", "arithmetic", "pre-algebra", "algebra", "algebra2", "geometry", "probability", "recreational-math"]},
        "mcat": {"related_subtopics": ["mcat", "arithmetic", "pre-algebra", "geometry", "probability", "recreational-math", "chemistry", "biology", "physics", "organic-chemistry", "health-and-medicine"]},
        "NCLEX-RN": {"related_subtopics": ["NCLEX-RN", "chemistry", "biology", "physics", "organic-chemistry", "health-and-medicine"]},
        "gmat": {"related_subtopics": ["gmat", "arithmetic", "pre-algebra", "algebra", "algebra2" "geometry", "probability", "chemistry", "biology", "physics", "organic-chemistry", "health-and-medicine", "history", "microeconomics", "macroeconomics"]},
        "cahsee-subject": {"related_subtopics": ["cahsee-subject", "early-math", "arithmetic", "pre-algebra", "geometry", "probability", "recreational-math"]},
        "iit-jee-subject": {"related_subtopics": ["iit-jee-subject", "arithmetic", "pre-algebra", "geometry", "differential-equations", "differential-calculus", "integral-calculus", "linear-algebra", "probability", "chemistry", "physics", "organic-chemistry"]},
        "ap-art-history": {"related_subtopics": ["ap-art-history", "art-history", "history"]},

        "CAS-biodiversity": {"related_subtopics": ["CAS-biodiversity", "chemistry", "biology", "physics", "organic-chemistry", "health-and-medicine", "Exploratorium"]},
        "Exploratorium": {"related_subtopics": ["Exploratorium", "chemistry", "biology", "physics", "organic-chemistry", "health-and-medicine", "CAS-biodiversity", "art-history", "music"]},
        "asian-art-museum": {"related_subtopics": ["asian-art-museum", "art-history", "history", "ap-art-history"]},
        "ssf-cci": {"related_subtopics": ["ssf-cci", "art-history", "history"]},
    }


    ### populate data exploiting structure of topic tree ###
    tree = generate_topic_tree()

    ######## DYNAMIC ALG #########

    data = {};

    ##
    # ITERATION 1 - grabs all immediate neighbors of each subtopic
    ##

    #array indices for the current topic and subtopic
    topic_index = 0
    subtopic_index = 0

    #for each topic 
    for topic in tree['children']:

        subtopic_index = 0

        #for each subtopic add the neighbors at distance 0 and 1 (at dist one has 2 for each)
        for subtopic in topic['children']:

            neighbors_dist_1 = get_neighbors_at_dist_1(topic_index, subtopic_index, tree)

            #add to data - distance 0 (itself) + distance 1
            data[ subtopic['id'] ] = { 'related_subtopics' : ([subtopic['id'] + ' 0'] + neighbors_dist_1) }
            subtopic_index+=1
            
        topic_index+=1

    ##
    # ITERATION 2 - grabs all subsequent neighbors of each subtopic via 
    # Breadth-first search (BFS)
    ##

    #loop through all subtopics currently in data dict
    for subtopic in data:
        related = data[subtopic]['related_subtopics'] # list of related subtopics (right now only 2)
        other_neighbors = get_subsequent_neighbors(related, data, subtopic)
        data[subtopic]['related_subtopics'] += other_neighbors ##append new neighbors


    ##
    # ITERATION 2.5 - Sort all results by increasing distance and to strip the final
    # result of all distance values in data (note that there are only 3 possible: 0,1,4).
    ##

    #for each item in data
    for subtopic in data:
        at_dist_4 = []          #array to hold the subtopic ids of recs at distance 4
        at_dist_lt_4 = []       #array to hold subtopic ids of recs at distance 0 or 1

        #for this item, loop through all recommendations
        for recc in data[subtopic]['related_subtopics']:
            if recc.split(" ")[1] == '4':   #if at dist 4, add to the array
                at_dist_4.append(recc.split(" ")[0]) 
            else:
                at_dist_lt_4.append(recc.split(" ")[0])

       
        sorted_related = at_dist_lt_4 + at_dist_4 #append later items at end of earlier
        data[subtopic]['related_subtopics'] = sorted_related



    return data



### 
# Returns a lookup table (a tree) that contains a list of related
# EXERCISES for each subtopic.
#
# @param data: a dicitonary with each subtopic and its related subtopics
###
def get_recommendation_tree(data):
    recommendation_tree = {}  # tree to return

    #loop through all subtopics passed in data
    for subtopic in data:
        recommendation_tree[str(subtopic)] = [] #initialize an empty list for the current s.t.

        related_subtopics = data[subtopic]['related_subtopics'] #list of related subtopic ids

        #loop through all of the related subtopics
        for rel_subtopic in related_subtopics:
            
            #make sure related is not an empty string (shouldn't happen but to be safe)
            if len(rel_subtopic) > 0:
                exercises = get_topic_exercises(rel_subtopic)

                for ex in exercises:
                    recommendation_tree[str(subtopic)].append(ex['id'])

    return recommendation_tree
      


###
# Returns a list of recommended exercise ids given a
# subtopic id. This will be the function called via the api
# endpoint.
#
# @param subtopic_id: the subtopic id (e.g. 'early-math')
###
def get_recommended_exercises(subtopic_id):

    if not subtopic_id:
        return []

    #get a recommendation lookup tree
    tree = get_recommendation_tree(generate_recommendation_data())

    #currently returning everything, perhaps we should just limit the
    #recommendations to a set amount??
    return tree[subtopic_id]



###
# Helper function for generating recommendation data using the topic tree.
# Returns a list of the neihbors at distance 1 from the specified subtopic.
#
# @param topic: the index of the topic that the subtopic belongs to (e.g. math, sciences)
#        subtopic: the index of the subtopic to find the neighbors for
###
def get_neighbors_at_dist_1(topic, subtopic, tree):
    neighbors = []  #neighbor list to be returned
    topic_index = topic #store topic index
    topic = tree['children'][topic] #subtree rooted at the topic that we are looking at
    #curr_subtopic = tree['children'][topic]['children'][subtopic]['id'] #id of topic passed in

    #pointers to the previous and next subtopic (list indices)
    prev = subtopic - 1 
    next = subtopic + 1

    #if there is a previous topic (neighbor to left)
    if(prev > -1 ):
        neighbors.append(topic['children'][prev]['id'] + ' 1') # neighbor on the left side

    #else check if there is a neighboring topic (left)    
    else:
        if (topic_index-1) > -1:
            neighbor_length = len(tree['children'][(topic_index-1)]['children'])
            neighbors.append(tree['children'][(topic_index-1)]['children'][(neighbor_length-1)]['id'] + ' 4')

        else:
            neighbors.append(' ') # no neighbor to the left

    #if there is a neighbor to the right
    if(next < len(topic['children'])):
        neighbors.append(topic['children'][next]['id'] + ' 1') # neighbor on the right side

    #else check if there is a neighboring topic (right)
    else:
        if (topic_index + 1) < len(tree['children']):
            #the 4 denotes the # of nodes in path to this other node, will always be 4
            neighbors.append(tree['children'][(topic_index+1)]['children'][0]['id'] + ' 4') 

        else:
            neighbors.append(' ') # no neighbor on right side


    return neighbors



###
# Performs Breadth-first search given recommendation data.
# Returns neighbors of a node in order of increasing distance.
# 
# @param nearest_neighbors: array holding the current left and right neighbors at dist 1 (always 2)
# @param data: dictionary of subtopics and their neighbors at distance 1
# @param curr: the current subtopic
###

def get_subsequent_neighbors(nearest_neighbors, data, curr):
    left_neigh = nearest_neighbors[1].split(' ')  # subtopic id and distance string of left neighbor
    right_neigh = nearest_neighbors[2].split(' ') # same but for right

    left = left_neigh[0]    #subtopic id of left
    right = right_neigh[0]  #subtopic id of right

    left_dist = -1          #dummy value
    right_dist = -1

    at_four_left = False    #boolean flag to denote that all other nodes to the left are at dist 4
    at_four_right = False   #same as above but for right nodes

    #checks, only applies to when left or right is ' ' (no neighbor)
    if  len(left_neigh) > 1:
        left_dist = left_neigh[1]           #distance of left neighbor
    else:
        left = ' '

    if len(right_neigh) > 1:
        right_dist = right_neigh[1]         #distance of right neighbor
    else:
        right = ' '

    other_neighbors = []

    # Loop while there are still neighbors
    while left != ' ' or right != ' ':

        if left == '':
            left= ' '

        # If there is a left neighbor, append its left neighbor
        if left != ' ':
            if data[left]['related_subtopics'][1] != ' ':

                #series of checks for each case
                #if all other nodes are at dist 4 (the first dist 4 was found)
                if(at_four_left):
                    new_dist = 4
                    at_four_left = True

                else:
                    #if immediate left node is 4
                    if data[ curr ]['related_subtopics'][1].split(' ')[1] == '4': 
                        at_four_left = True
                        new_dist = 4
                    elif data[left]['related_subtopics'][1].split(' ')[1] == '4': #if the next left neighbor is at dist 4
                        at_four_left = True
                        new_dist = 4
                    else: #this means that the next left node is at dist 1
                        new_dist = 1

                other_neighbors.append(data[left]['related_subtopics'][1].split(' ')[0] + ' ' + str(new_dist))
            left = data[left]['related_subtopics'][1].split(' ')[0]
        
        if right == '':
            right = ' '

        # Repeat for right neighbor
        if right != ' ':
            if data[right]['related_subtopics'][2] != ' ':

                #series of checks for each case
                #if all other nodes are at dist 4 (the first dist 4 was found)
                if(at_four_right):
                    new_dist = 4
                    at_four_right = True

                else:
                    #if immediate right node is 4
                    if data[ curr ]['related_subtopics'][2].split(' ')[1] == '4':           
                        new_dist = 4
                    elif data[right]['related_subtopics'][2].split(' ')[1] == '4': #if the next right neighbor is at dist 4
                        new_dist = 4
                    else: #this means that the next right node is at dist 1
                        new_dist = 1

                if new_dist == 4:
                    at_four_right = True

                other_neighbors.append(data[right]['related_subtopics'][2].split(' ')[0] + ' ' + str(new_dist))
            right = data[right]['related_subtopics'][2].split(' ')[0]

    return other_neighbors

### END content recommendation ###