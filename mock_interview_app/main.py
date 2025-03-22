import json
import pdfplumber
import fitz
import mimetypes
import random
import cv2
import numpy as np
import logging
from fastapi import FastAPI, HTTPException, status, File, UploadFile, Request, Body
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from typing import Annotated, Dict, Optional, List, Union
import uvicorn


# Import the improved functions from our evaluation module
# Assuming the previous code is saved in a module named 'evaluation_system'
from api_request import (
    prepare_prompt, 
    get_questions, 
    prepare_prompt_for_answercheck, 
    get_evaluation,
    evaluate_candidate,
    EvaluationError,
    get_feedback_for_score
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Create the FastAPI app
app = FastAPI(
    title="Candidate Evaluation API",
    description="API for generating interview questions and evaluating candidate answers",
    version="1.0.0"
)

@app.post("/questions", response_description="Questions generated using LangChain with Groq")
async def langchain_questions(
    file: Annotated[UploadFile, File(description="A file read as UploadFile")], 
    data: str = None):
    """Generate interview questions based on resume and specified parameters.
    
    Args:
        file: PDF resume file
        data: JSON string containing techStack, difficultyLevel, and questionCount
        
    Returns:
        Dictionary containing metadata and generated questions
    Examples:
        data:{
            "techStack": "Python, JavaScript",
            "difficultyLevel": 3,
            "questionCount": 5
        }
    """
    try:
        # Validate file type
        content_type = file.content_type
        if content_type not in ["application/pdf", "application/x-pdf"]:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, 
                detail="File is not a valid PDF"
            )
        
        # Read and parse the uploaded file
        pdf_data = await file.read()
        
        # Parse JSON data
        try:
            json_data = json.loads(data) if data else {}
            if not json_data:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Missing required parameters in JSON data"
                )
            
            # Validate required fields
            required_fields = ['techStack', 'difficultyLevel', 'questionCount']
            for field in required_fields:
                if field not in json_data:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Missing required field: {field}"
                    )
        except json.JSONDecodeError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid JSON data format"
            )
            
        processed_data = {
            "file_size": len(pdf_data), 
            "file_name": file.filename, 
            "json_data": json_data
        }

        # Extract text from PDF using PyMuPDF (fitz)
        try:
            pdf_doc = fitz.open(stream=pdf_data, filetype="pdf")
            resume_text = ""
            for page in pdf_doc:
                resume_text += page.get_text()
                
            if not resume_text.strip():
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Could not extract text from PDF. The file may be empty or corrupted."
                )
        except Exception as e:
            logger.error(f"Error processing PDF: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to process PDF: {str(e)}"
            )

        # Generate questions
        try:
            prompt = prepare_prompt(
                resume=resume_text,
                tech_stack=json_data['techStack'],
                difficulty=json_data['difficultyLevel'],
                question_count=json_data['questionCount']
            )
            
            logger.info(f"Generated prompt for questions, length: {len(prompt)}")
            questions = get_questions(prompt)
            
            if not questions:
                logger.warning("No questions were generated")
                return JSONResponse(
                    status_code=status.HTTP_204_NO_CONTENT,
                    content={"message": "No questions could be generated. Try adjusting parameters."}
                )
            
            return {
                "metadata": processed_data,
                "questions": questions,
                "count": len(questions)
            }
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e)
            )
        except Exception as e:
            logger.error(f"Error generating questions: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to generate questions: {str(e)}"
            )
            
    except HTTPException:
        # Re-raise HTTP exceptions to preserve their status codes
        raise
    except Exception as e:
        logger.error(f"Unexpected error in langchain_questions: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An unexpected error occurred: {str(e)}"
        )

@app.post("/check-answers", response_description="Checking answers using LangChain with Groq")
async def check_answers(json_data: dict = Body(...)):
    """Evaluate candidate answers to interview questions.
    
    Args:
        json_data: Dictionary mapping questions to answers
        
    Returns:
        Dictionary containing evaluation score and feedback
    """
    try:
        # Validate input
        if not isinstance(json_data, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Data must be a dictionary mapping questions to answers"
            )
            
        if not json_data:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No question-answer pairs provided"
            )
            
        # Check if any answers are empty
        empty_answers = [q for q, a in json_data.items() if not a or not a.strip()]
        if empty_answers:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Empty answers provided for {len(empty_answers)} questions"
            )

        # Generate evaluation prompt and get score
        try:
            prompt = prepare_prompt_for_answercheck(json_data)
            score = get_evaluation(prompt)
            feedback = get_feedback_for_score(score)
            
            return {
                "score": score,
                "feedback": feedback,
                "evaluated_answers": len(json_data),
                "status": "success"
            }
        except EvaluationError as e:
            logger.error(f"Evaluation error: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Evaluation failed: {str(e)}"
            )
            
    except HTTPException:
        # Re-raise HTTP exceptions to preserve their status codes
        raise
    except Exception as e:
        logger.error(f"Unexpected error in check_answers: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An unexpected error occurred: {str(e)}"
        )

@app.post("/complete-evaluation", response_description="End-to-end candidate evaluation")
async def complete_evaluation(
    file: Annotated[UploadFile, File(description="Candidate resume as PDF")], 
    tech_stack: str = Body(...),
    difficulty: int = Body(...),
    question_count: int = Body(...),
    answers: Dict[str, str] = Body(...)):
    """Perform end-to-end evaluation of a candidate.
    
    This endpoint combines resume analysis, question generation, and answer evaluation.
    
    Args:
        file: PDF resume file
        tech_stack: Technologies to focus on
        difficulty: Difficulty level (1-5)
        question_count: Number of questions to generate
        answers: Dictionary mapping questions to answers
        
    Returns:
        Complete evaluation results
    """
    try:
        # Validate file type
        content_type = file.content_type
        if content_type not in ["application/pdf", "application/x-pdf"]:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, 
                detail="File is not a valid PDF"
            )
            
        # Read and extract text from PDF
        pdf_data = await file.read()
        try:
            pdf_doc = fitz.open(stream=pdf_data, filetype="pdf")
            resume_text = ""
            for page in pdf_doc:
                resume_text += page.get_text()
                
            if not resume_text.strip():
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Could not extract text from PDF. The file may be empty or corrupted."
                )
        except Exception as e:
            logger.error(f"Error processing PDF: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to process PDF: {str(e)}"
            )
            
        # Validate inputs
        if not tech_stack or not tech_stack.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Tech stack cannot be empty"
            )
            
        try:
            difficulty = int(difficulty)
            if not 1 <= difficulty <= 5:
                raise ValueError("Difficulty must be between 1 and 5")
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Difficulty must be an integer between 1 and 5"
            )
            
        try:
            question_count = int(question_count)
            if not 1 <= question_count <= 20:
                raise ValueError("Question count must be between 1 and 20")
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Question count must be an integer between 1 and 20"
            )
            
        if not answers:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No answers provided for evaluation"
            )
            
        # Perform the evaluation
        result = evaluate_candidate(
            resume=resume_text,
            tech_stack=tech_stack,
            difficulty=difficulty,
            question_count=question_count,
            answers=answers
        )
        
        if result["status"] == "error":
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=result["error"]
            )
            
        return result
        
    except HTTPException:
        # Re-raise HTTP exceptions to preserve their status codes
        raise
    except Exception as e:
        logger.error(f"Unexpected error in complete_evaluation: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An unexpected error occurred: {str(e)}"
        )

# Add a simple root endpoint for API health check
@app.get("/", response_description="API Status")
async def root():
    return {"status": "online", "message": "Candidate Evaluation API is running"}

