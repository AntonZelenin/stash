# Public entry point: HTTP API → API Lambda (app.aws_lambda.handler) →
# Mangum → FastAPI.
#
# One catch-all `$default` route proxies every method and path to the
# function, and the `$default` stage serves it at the root (no stage prefix
# in the path), so FastAPI routes exactly as it does behind uvicorn.
#
# No CORS configuration here: FastAPI's CORSMiddleware answers preflights
# from CORS_ALLOWED_ORIGINS (var.api_cors_allowed_origins). API Gateway CORS
# would answer them itself and override the application's headers.

resource "aws_apigatewayv2_api" "main" {
  name          = local.name_prefix
  description   = "Stash API"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "api" {
  api_id = aws_apigatewayv2_api.main.id

  integration_type   = "AWS_PROXY"
  integration_method = "POST"
  integration_uri    = aws_lambda_function.main["api"].invoke_arn
  # The HTTP API event format; Mangum detects it.
  payload_format_version = "2.0"
  # API Gateway's maximum; the function's own timeout is at most this too.
  timeout_milliseconds = 30000
}

resource "aws_apigatewayv2_route" "default" {
  api_id    = aws_apigatewayv2_api.main.id
  route_key = "$default"
  target    = "integrations/${aws_apigatewayv2_integration.api.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.main.id
  name        = "$default"
  auto_deploy = true
}

resource "aws_lambda_permission" "api_gateway" {
  statement_id  = "AllowApiGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.main["api"].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.main.execution_arn}/*/*"
}
