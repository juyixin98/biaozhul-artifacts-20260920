// Synthetic local fixtures for the contract-diff matrix.
// No real services or accounts: every value is hand-authored.

const OLD = `
openapi: 3.1.0
info:
  title: Pets API
  version: "1.0.0"
paths:
  /pets:
    get:
      operationId: listPets
      parameters:
        - name: tag
          in: query
          required: false
          schema:
            type: string
            enum: [cat, dog]
            default: cat
        - name: trace
          in: header
          required: false
          schema:
            type:
              - string
              - 'null'
        - name: X-Tenant
          in: header
          required: false
          schema:
            type: string
      responses:
        '200':
          description: list
          content:
            application/json:
              schema:
                type: object
                required: [items]
                additionalProperties: false
                properties:
                  items:
                    type: array
                    items:
                      $ref: '#/components/schemas/Pet'
        '404':
          description: not found
    post:
      operationId: createPet
      requestBody:
        required: false
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/PetCreate'
      responses:
        '201':
          description: created
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Pet'
  /pets/{id}:
    get:
      operationId: getPet
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: string
      responses:
        '200':
          description: ok
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Pet'
  /legacy:
    get:
      operationId: legacy
      responses:
        '200':
          description: ok
components:
  schemas:
    Pet:
      type: object
      required: [id, name, tags]
      properties:
        id:
          type: string
        name:
          type:
            - string
            - 'null'
        note:
          type: string
        tags:
          type: array
          items:
            type: string
        links:
          type: array
          items:
            $ref: '#/components/schemas/Link'
    Link:
      type: object
      required: [href]
      properties:
        href:
          type: string
        next:
          $ref: '#/components/schemas/Link'
    PetCreate:
      type: object
      required: [name]
      properties:
        name:
          type: string
        kind:
          type: string
          enum: [cat, dog, bird]
        note:
          type:
            - string
            - 'null'
`

const NEW = `
openapi: 3.1.0
info:
  title: Pets API
  version: "2.0.0"
paths:
  /pets:
    get:
      operationId: listPets
      parameters:
        # enum EXTENDED (request-safe) but default CHANGED
        - name: tag
          in: query
          required: false
          schema:
            type: string
            enum: [cat, dog, bird]
            default: dog
        # nullability REMOVED on a request parameter
        - name: trace
          in: header
          required: true
          schema:
            type: string
        # SAME NAME, DIFFERENT LOCATION: header -> query
        - name: X-Tenant
          in: query
          required: false
          schema:
            type: string
        # newly required parameter
        - name: limit
          in: query
          required: true
          schema:
            type: integer
      responses:
        '200':
          description: list
          content:
            application/json:
              schema:
                type: object
                required: [items]
                properties:
                  items:
                    type: array
                    items:
                      $ref: '#/components/schemas/Pet'
                  # newly added response property while clients forbid extras
                  page:
                    type: integer
        # 404 removed, new 500 introduced
        '500':
          description: error
    post:
      operationId: createPet
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/PetCreate'
      responses:
        '201':
          description: created
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Pet'
  /pets/{id}:
    get:
      operationId: getPet
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: string
      responses:
        '200':
          description: ok
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Pet'
  # /legacy operation removed entirely
components:
  schemas:
    Pet:
      type: object
      required: [id, name]        # tags no longer required on responses
      additionalProperties: false # note removed + extras forbidden: uncertain
      properties:
        id:
          type:
            - string
            - 'null'            # NEW may emit null id: OLD clients reject it
        name:
          type: string            # nullability removed (safe: server subset)
        tags:
          type: array
          items:
            type: string
        links:
          type: array
          items:
            $ref: '#/components/schemas/Link'
    Link:
      type: object
      required: [href]
      properties:
        href:
          type: string
        next:
          $ref: '#/components/schemas/Link'
    PetCreate:
      type: object
      required: [name, kind]       # kind became required
      properties:
        name:
          type: string
        kind:
          type: string
          enum: [cat, dog]         # enum NARROWED on input: bird removed
        note:
          type: string            # null removed on input
      x-internal-hint: keep-fast  # extension: must NOT be judged breaking
`

export const FIXTURES = { OLD, NEW };
