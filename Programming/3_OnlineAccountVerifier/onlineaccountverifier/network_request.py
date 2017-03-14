from onlineaccountverifier.network_commands import *
from onlineaccountverifier.network_exceptions import *
from twisted.internet.protocol import Factory
from twisted.python.failure import Failure
import os
from twisted.internet import reactor, defer, endpoints
from twisted.internet.endpoints import TCP4ClientEndpoint, connectProtocol
from twisted.protocols.amp import AMP
import pickle, pprint
import signatures.token_request as token_request
from Crypto.Hash import SHA256


class RequestHandler(amp.AMP):

    def __init__(self):
        super().__init__()
        self.twisted_ballotregulator_port = int(os.environ['TWISTED_BALLOTREGULATOR_PORT'])
        self.twisted_ballotregulator_ip = str(os.environ['TWISTED_BALLOTREGULATOR_IP'])

    @Request_SignBlindToken.responder
    def request_sign_blind_token(self, user_id, ballot_id, blind_token):
        """
        http://twistedmatrix.com/documents/12.1.0/core/howto/defer.html#class

        :param user_id:
        :param ballot_id:
        :param blind_token:
        :return:
        """
        print('[RequestHandler - request_sign_blind_token] Received request : user_id:%d, ballot_id:%d, blind_token:%s...' % (user_id, ballot_id, str(blind_token)[0:20] ))

        databasequery = self.factory.get_databasequery()

        # First we need to query the OnlineBallotRegulator for the 'user_id'

        def searchuser_onconected(ampProto):
            return ampProto.callRemote(Request_RetrieveBallots, user_id=user_id)

        def searchuser_callremote_errback(failure):
            print("There was an error in the remote call", type(failure))
            raise failure.raiseException()

        print('[RequestHandler - request_sign_blind_token] Connecting to ballotregulator - port=%s, ip=%s'
              % (self.twisted_ballotregulator_ip, self.twisted_ballotregulator_port))

        searchuser_destination      = TCP4ClientEndpoint(reactor, self.twisted_ballotregulator_ip, self.twisted_ballotregulator_port)
        searchuser_connectcall      = connectProtocol(searchuser_destination, AMP())
        searchuser_results          = searchuser_connectcall.addCallback(searchuser_onconected).addErrback(searchuser_callremote_errback)

        # Now we need to format the returned results.

        def format_searchuser_results(pickled_result):

            # First unpickle the results.
            result = pickle.loads(pickled_result['ok'])

            # Transform the list results into a dictionary.
            record_list = []
            for record in result:
                mapper = {
                    'user_id': record[0],
                    'ballot_id': record[1],
                    'timestamp': record[2],
                    'ballot_name': record[3],
                    'ballot_address': record[4]
                }
                # Append each row's dictionary to a list
                record_list.append(mapper)

            # return record_list
            return record_list

        def format_searchuser_results_errback(failure):
            print("[RequestHandler - request_sign_blind_token - format_searchuser_results_errback] There was an error formatting the results", type(failure))
            raise failure.raiseException()

        searchuser_results_format_result  = searchuser_results.addCallback(format_searchuser_results).addErrback(format_searchuser_results_errback)

        # Now, lets check that the voter_id is registered for the ballot_id

        def checkvalid_userid_ballotid(results_list):
            found = False
            for record in results_list:
                if record['ballot_id'] == ballot_id:
                    found = True
                    print("[RequestHandler - request_sign_blind_token - checkvalid_userid_ballotid] user_id=%s is registered for ballot_id=%s"
                          % (user_id, ballot_id))
                    break

            if not found:
                raise UserNotRegisterdForBallot(user_id,ballot_id)

            return found # Return something though we dont need to.

        def checkvalid_userid_ballotid_errback(failure):
            print("[RequestHandler - request_sign_blind_token - checkvalid_userid_ballotid_errback] There was an error when checking the ballot & user id's")
            raise failure.raiseException()


        checkvalid_userid_ballotid_result = searchuser_results_format_result.addCallback(checkvalid_userid_ballotid).addErrback(checkvalid_userid_ballotid_errback)

        # Okay thats good, is this the first time the onlineaccountverifier is seeing this combination of user_id & ballot_id?

        def checkfirsttime_userid_ballotid(userid_found_in_onlineballotregulator):

            query = databasequery.retrieve_request_sign(user_id)

            def checkReturnedQuery(pickled_result):
                # First unpickle the results.
                results_list = pickle.loads(pickled_result['ok'])

                 # Transform the list results into a dictionary.
                record_list = []
                for record in results_list:
                    mapper = {
                        'token_request_id': record[0],
                        'blind_token': record[1],
                        'user_id': record[2],
                        'ballot_id': record[3],
                        'timestamp': record[4]
                    }
                    # Append each row's dictionary to a list
                    record_list.append(mapper)


                for record in record_list:
                    if record['ballot_id'] == ballot_id:
                        print("[RequestHandler - request_sign_blind_token - checkfirsttime_userid_ballotid] user_id='%s' is registered for ballot_id='%s'"
                              % (user_id, ballot_id))
                        raise UserAlreadySubmittedTokenForThisBallot(user_id,ballot_id, record['blind_token'] )

                return True # Return something though we dont need to.

            return query.addCallback(checkReturnedQuery)

        def checkfirsttime_userid_ballotid_errback(failure):
            print("[RequestHandler - request_sign_blind_token - checkvalid_userid_ballotid_errback] There was an error checking the ballot & user id's")
            raise failure.raiseException()

        checkfirsttime_userid_ballotid_result   = checkvalid_userid_ballotid_result.addCallback(checkfirsttime_userid_ballotid).addErrback(checkfirsttime_userid_ballotid_errback)

        # All of our checks are complete, lets sign the token.

        def sign_blindtoken(checkfirsttime_userid_ballotid):
            from twisted.internet import defer
            print("[RequestHandler - request_sign_blind_token - sign_blindtoken] Signing the token '%s...'for the ballot '%s'" % (str(blind_token)[0:20], ballot_id))

            d = defer.Deferred()

            signed_blind_token = token_request.sign_blind_token( blind_token, ballot_id )

            d.callback(signed_blind_token)

            return d

        def sign_blindtoken_errback(failure):
            print("[RequestHandler - request_sign_blind_token - sign_blindtoken_errback] There was an error signing the token.")
            raise failure.raiseException()

        sign_blindtoken_result = checkfirsttime_userid_ballotid_result.addCallback(sign_blindtoken).addErrback(sign_blindtoken_errback)

        # Okay, signing worked. Lets save this request.

        def save_token_request(signed_blind_token):

            print("[RequestHandler - request_sign_blind_token - save_token_request] Saving the register token request of \n    "
                  "user_id='%s' for ballot_id='%s' with signed_blind_ballot='%s...'" % (user_id, ballot_id, str(signed_blind_token)[0:20]) )

            # We should save our processed request.
            hash = SHA256.new()
            hash.update(str(blind_token).encode())
            blind_token_hash = hash.hexdigest()

            defer = databasequery.register_token_request(blind_token_hash, user_id, ballot_id)

            # Cool, saving worked. Return singed token to user.

            def return_result(ignored):
                print("[RequestHandler - request_sign_blind_token - returnResult] Computation completed, returning signed token to the client. ")

                # Pickle the signed token to return over the wire
                encoded_signed_blind_token = pickle.dumps(signed_blind_token)

                return { 'ok' : encoded_signed_blind_token }

            def return_result_errback(failure):
                print("[RequestHandler - request_sign_blind_token - return_result_errback] There was an error saving the token.")
                raise failure.raiseException()

            defer.addCallback(return_result).addErrback(return_result_errback)

            return defer


        def save_token_request_errback(failure):
            print("[RequestHandler - request_sign_blind_token - save_token_request_errback] There was an error saving the token.")
            raise failure.raiseException()

        save_token_request = sign_blindtoken_result.addCallback(save_token_request).addErrback(save_token_request_errback)

        return save_token_request


    @Request_RegisterAddressToBallot.responder
    def request_register_address_to_ballot(self, ballot_id, pickled_signed_token, pickled_token, pickled_voter_address):
        """
        Called after obtaining a signed_blind_token from `request_sign_blind_token`. This will allow you to
        register an ethereum address to vote on a ballot contract.

        :param ballot_id:
        :param pickled_signed_token:
        :param pickled_token:
        :param pickled_voter_address:
        :return:
        """

        # First lets unpickle
        signed_token = pickle.loads(pickled_signed_token)
        token = pickle.loads(pickled_token)
        voter_address = pickle.loads(pickled_voter_address)

        print('[RequestHandler - request_register_address_to_ballot] Received request : ballot_id:%d, token:%s, signed_token:%s...' % (ballot_id, token, str(signed_token)[0:20]))

         # First we need to check that we received a validly signed token.

        def check_token_signed_for_ballot(pickled_result):
            result = token_request.check_token_signed_for_ballot(signed_token, token, ballot_id)
            return result

        def check_token_signed_for_ballot_errback(failure):
            print("[RequestHandler - request_sign_blind_token - format_searchuser_results_errback] There was an error formatting the results", type(failure))
            raise failure.raiseException()

        top_defer = defer.Deferred()
        check_token_signed_for_ballot_result = top_defer.addCallback(check_token_signed_for_ballot).addErrback(check_token_signed_for_ballot_errback)
        top_defer.callback(None) # Start our defered calls

        # Next we need to check that this is the first time we are seeing this token + (voter_address & ballot_id)

        def check_first_time_seeing_token_ballotid_voteraddress(prev_result):
            pass

        def check_first_time_seeing_token_ballotid_voteraddress_errback(failure):
            pass

        check_first_time_seeing_token_ballotid_voteraddress_result = check_token_signed_for_ballot_result.addCallback(check_first_time_seeing_token_ballotid_voteraddress).addErrback(check_first_time_seeing_token_ballotid_voteraddress_errback)

        # Get the ballot information from the ballot_id

        def get_ballot_information(prev_result):

            destination = TCP4ClientEndpoint(reactor, self.twisted_ballotregulator_ip, self.twisted_ballotregulator_port)
            requestsearchuser_deferred = connectProtocol(destination, AMP())

            def requestsearchuser_connected(ampProto):
                return ampProto.callRemote(Request_RetrieveAllBallots)
            requestsearchuser_deferred.addCallback(requestsearchuser_connected)

            def done(result):
                '''
                Returns the record ascociated with the ballot_id
                :param result:
                :return:
                '''
                unpickled_result = pickle.loads(result['ok'])

                # Transform the list results into a dictionary.
                record_list = []
                for record in unpickled_result:
                    mapper = {}
                    mapper['ballot_id'] = record[0]
                    mapper['ballot_name'] = record[1]
                    mapper['ballot_address'] = record[2]
                    mapper['timestamp'] = record[3]
                    # Append each row's dictionary to a list
                    record_list.append(mapper)

                # Check the available ballots for *our* ballot
                for record in record_list:
                    if __name__ == '__main__':
                        if record['ballot_id'] == ballot_id:
                            return record

                # If we reach here we havent found *our* ballot in the list available
                raise BallotNotAvailable(ballot_id)

            parsed_results = requestsearchuser_deferred.addCallback(done)

            return parsed_results

        def get_ballot_information_errback(failure):
            raise failure.raiseException()

        get_ballot_information_result = check_first_time_seeing_token_ballotid_voteraddress_result.addCallback(get_ballot_information).addErrback(get_ballot_information_errback)

        # Register on the ethereum contract.

        def register_voteraddress_ethereumcontract(prev_result):
            pass

        def register_voteraddress_ethereumcontract_errback(failure):
            pass

        register_voteraddress_ethereumcontract_result = get_ballot_information_result.addCallback(register_voteraddress_ethereumcontract).addErrback(register_voteraddress_ethereumcontract_errback)

        # Save our details to register_vote table

        def save_vote_registration(prev_result):
            pass

        def save_vote_registration_errback(failure):
            pass

        save_vote_registration_result = register_voteraddress_ethereumcontract_result.addCallback(save_vote_registration).addErrback(save_vote_registration_errback)


        def return_results_to_client(res):
            print("[RequestHandler - request_sign_blind_token - return_results_to_client] Returning 'ok' to client.")
            return { 'ok' : True }

        return save_vote_registration_result.addCallback(return_results_to_client)

class MyServerFactory(Factory):
    protocol = RequestHandler

    def __init__(self, databasequery):
        self.databasequery = databasequery

    def get_databasequery(self):
        return self.databasequery