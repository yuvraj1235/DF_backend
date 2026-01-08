import csv
import logging
import time
from decimal import Decimal

import requests as r
from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import MultipleObjectsReturned, ObjectDoesNotExist
from django.http import HttpResponse
from django.utils import timezone
from knox.models import AuthToken
from rest_framework import generics, status
from rest_framework.decorators import permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from google.oauth2 import id_token
from google.auth.transport import requests as google_auth_requests
from decouple import config

from .models import Round, Player, Clue, duration
from .serializers import CreateUserSerializer, RoundSerializer, PlayerSerializer

logger = logging.getLogger(__name__)

# --- Helper Functions ---

def check_duration(username):
    try:
        tm = timezone.now()
        obj = duration.objects.all().first()
        if not obj:
            return False
        
        player = Player.objects.get(name=username)
        if player.isStaff:
            return False
        
        if tm > obj.start_time and tm < obj.end_time:
            return False
        return True
    except Exception:
        return True

def isHidden():
    obj = duration.objects.all().first()
    return obj.leaderboard_hide == 1 if obj else False

def verifyGoogleToken(token):
    CLIENT_ID = config('CLIENT_ID', cast=str)
    try:
        idinfo = id_token.verify_oauth2_token(
            token, google_auth_requests.Request(), CLIENT_ID)

        if idinfo['iss'] not in ['accounts.google.com', 'https://accounts.google.com']:
            raise ValueError('Wrong issuer.')

        return {
            "email": idinfo['email'],
            "username": idinfo['email'],
            "first_name": idinfo.get('name', 'User'),
            "image": idinfo.get('picture', ''),
            "status": 200
        }
    except Exception as e:
        logger.error(f"Google Auth Error: {str(e)}")
        return {"status": 404, "message": str(e)}

def verifyGithubToken(accessCode):
    try:
        tokenurl = "https://github.com/login/oauth/access_token"
        params = {
            "client_id": config('GITHUB_CLIENT_ID'),
            "client_secret": config('GITHUB_CLIENT_SECRET'),
            "code": accessCode,
        }
        res = r.post(url=tokenurl, params=params, headers={"Accept": "application/json"}).json()
        
        if "access_token" not in res:
            return {"status": 404, "message": "Invalid Github Code"}

        headers = {"Authorization": f"Bearer {res['access_token']}"}
        userinfo = r.get("https://api.github.com/user", headers=headers).json()
        emails = r.get("https://api.github.com/user/emails", headers=headers).json()
        
        return {
            "email": emails[0]['email'],
            "username": emails[0]['email'],
            "first_name": userinfo.get('name', 'Github User'),
            "image": userinfo.get('avatar_url', ''),
            "status": 200
        }
    except Exception:
        return {"status": 404}

def verifyUser(email):
    return Player.objects.filter(email=email).exists()

def centrePoint(round_obj):
    clues = Clue.objects.filter(round=round_obj)
    if not clues.exists():
        return [0.0, 0.0]
    
    x = Decimal(0.0)
    y = Decimal(0.0)
    for clue in clues:
        pos = clue.getPosition()
        x += Decimal(pos[0])
        y += Decimal(pos[1])
    
    count = clues.count()
    return [float(x/count), float(y/count)]

# --- API Views ---

def LeaderBoardDownload(request):
    if request.GET.get("password") == config('DOWNLOAD', cast=str):
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="leaderboards.csv"'
        writer = csv.writer(response)
        writer.writerow(["Name", "Email", "Score"])
        for player in Player.objects.order_by("-score", "submit_time"):
            if not player.isStaff:
                writer.writerow([player.first_name, player.email, player.score])
        return response
    return HttpResponse("Unauthorized", status=403)

@permission_classes([AllowAny])
class leaderboard(generics.GenericAPIView):
    def get(self, request):
        if isHidden():
            return Response({"standings": [], "safe": False, "status": 203})
        
        players = Player.objects.filter(isStaff=False).order_by("-score", "submit_time")
        data = []
        for rank, p in enumerate(players, 1):
            data.append({
                "name": p.first_name,
                "rank": rank,
                "score": p.score,
                "image": p.imageLink,
            })
        return Response({"standings": data, "safe": False, "status": 200})

@permission_classes([AllowAny])
class Register(generics.GenericAPIView):
    serializer_class = CreateUserSerializer

    def post(self, request, *args, **kwargs):
        # ... (Verify Google/Github token logic) ...
        
        if verifyUser(res['email']) == False:
            # NEW USER: Save them and create their ONLY token
            serializer = self.get_serializer(data=res)
            serializer.is_valid(raise_exception=True)
            user = serializer.save()
            player = Player.objects.create(...)
            
            # Create token for the first time
            _, token_str = AuthToken.objects.create(user)
            return Response({"user": serializer.data, "token": token_str, "status": 200})
        else:
            # EXISTING USER: Treat as login (see Login logic below)
            return self.handle_existing_user(res['email'])
@permission_classes([AllowAny])
class Login(generics.GenericAPIView):
    serializer_class = PlayerSerializer

    def post(self, request, *args, **kwargs):
        # ... (Your existing provider verification logic) ...

        try:
            email = res.get('email')
            user = User.objects.filter(email=email).first()
            player = Player.objects.filter(email=email).first()

            if user and player:
                # 1. DELETE all existing tokens for this specific user
                AuthToken.objects.filter(user=user).delete() #
                
                # 2. CREATE a single fresh token
                _, token_str = AuthToken.objects.create(user) #
                
                return Response({
                    "user": self.get_serializer(player).data,
                    "token": token_str,
                    "status": 200
                })
            return Response({"message": "Not registered"}, status=401)
        except Exception as e:
            return Response({"message": "Internal Server Error"}, status=500)
    serializer_class = PlayerSerializer

    def post(self, request, *args, **kwargs):
        # ... (Verify Google/Github token logic) ...

        email = res.get('email')
        if verifyUser(email):
            user = User.objects.get(email=email)
            player = Player.objects.get(email=email)
            
            # CHECK FOR EXISTING TOKEN
            # We look for the most recent valid token instead of creating one
            existing_token = AuthToken.objects.filter(user=user).first()
            
            if not existing_token:
                # If for some reason they don't have one, create it now
                _, token_str = AuthToken.objects.create(user)
            else:
                # Logic to get the actual token string is tricky with Knox 
                # because Knox hashes tokens in the DB. 
                # Standard practice: Create one fresh, delete old ones.
                AuthToken.objects.filter(user=user).delete()
                _, token_str = AuthToken.objects.create(user)

            serializer = self.get_serializer(player)
            return Response({"user": serializer.data, "token": token_str, "status": 200})
@permission_classes([IsAuthenticated])
class getRound(APIView):
    def get(self, request):
        if check_duration(request.user.username):
            return Response({"status": 410, "message": "Quiz not active"})
        
        try:
            player = Player.objects.get(name=request.user.username)
            curr_round = Round.objects.get(round_number=player.roundNo)
            dur = duration.objects.all().first()
            
            if dur and player.roundNo > dur.max_question:
                return Response({"message": "Finished!", "status": 404})

            return Response({
                "question": RoundSerializer(curr_round).data, 
                "centre": centrePoint(curr_round), 
                "status": 200
            })
        except Exception:
            return Response({"message": "Finished!", "status": 404})

@permission_classes([IsAuthenticated])
class checkRound(APIView):
    def post(self, request):
        if check_duration(request.user.username):
            return Response({"status": 410})
        try:
            player = Player.objects.get(name=request.user.username)
            round_obj = Round.objects.get(round_number=player.roundNo)

            if round_obj.checkAnswer(request.data.get("answer")):
                dur = duration.objects.all().first()
                if not (dur and dur.leaderboard_freeze):
                    player.score += 10
                
                player.roundNo += 1
                player.submit_time = timezone.now()
                player.save()
                return Response({"status": 200})
            return Response({"status": 500, "message": "Wrong Answer"})
        except Exception:
            return Response({"status": 404})

@permission_classes([IsAuthenticated])
class getuserscore(APIView):
    def get(self, request):
        try:
            player = Player.objects.get(name=request.user.username)
            all_players = Player.objects.filter(isStaff=False).order_by("-score", "submit_time")
            
            rank = 0
            for i, p in enumerate(all_players, 1):
                if p.email == player.email:
                    rank = i
                    break
                    
            return Response({
                "status": 200,
                "score": player.score,
                "rank": rank,
                "name": player.first_name,
                "email": player.email
            })
        except Player.DoesNotExist:
            return Response({"status": 404, "message": "User not found"})

@permission_classes([IsAuthenticated])
class getClue(APIView):
    def get(self, request):
        if check_duration(request.user.username):
            return Response({"status": 410})
        try:
            player = Player.objects.get(name=request.user.username)
            round_obj = Round.objects.get(round_number=player.roundNo)
            clues = Clue.objects.filter(round=round_obj)
            
            response_data = []
            for c in clues:
                is_solved = player.checkClue(c.id)
                item = {"id": c.id, "question": c.question, "solved": is_solved}
                if is_solved:
                    item["position"] = c.getPosition()
                response_data.append(item)
            return Response({"clues": response_data, "status": 200})
        except Exception:
            return Response({"status": 404})

@permission_classes([IsAuthenticated])
class putClue(APIView):
    def post(self, request):
        if check_duration(request.user.username):
            return Response({"status": 410})
        try:
            player = Player.objects.get(name=request.user.username)
            clue = Clue.objects.get(pk=int(request.data.get("clue_id")))
            
            if clue.checkAnswer(request.data.get("answer")):
                player.putClues(clue.pk)
                player.save()
                return Response({"status": 200, "position": clue.getPosition()})
            return Response({"status": 500, "message": "Wrong Answer"})
        except Exception:
            return Response({"status": 404})