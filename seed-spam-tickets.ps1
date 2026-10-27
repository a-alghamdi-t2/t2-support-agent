# Adds two spam emails from one new sender to a LOCAL copy of the T2 Ticketing
# System, the way its Gmail sync creates them (as seed-email-tickets.ps1 does):
# "Unclassified Email", no queue, Open, RequiresClassification = 1, sender
# Pending. For testing Approve: Spam, which blocks the sender and closes all of
# their open tickets.
#
# Local only. It refuses to run against anything but (localdb).
#
#   powershell -ExecutionPolicy Bypass -File .\seed-spam-tickets.ps1
#
# Running it again skips the emails that are already there.

param(
    [string]$Database = "T2TicketsLocal"
)

$ErrorActionPreference = "Stop"
$connectionString = "Server=(localdb)\MSSQLLocalDB;Database=$Database;Integrated Security=True;TrustServerCertificate=True"
if ($connectionString -notmatch '^Server=\(localdb\)') { throw "Local databases only." }

$sql = @'
SET NOCOUNT ON;
SET XACT_ABORT ON;
BEGIN TRANSACTION;

DECLARE @now datetime2 = SYSUTCDATETIME();
DECLARE @product int = (SELECT Id FROM Products WHERE Code = 'EMAIL');
DECLARE @org uniqueidentifier = (SELECT Id FROM Organizations WHERE DomainName = 'pending-contacts.invalid');
IF @product IS NULL OR @org IS NULL
    THROW 50000, 'Run seed-email-tickets.ps1 first: no EMAIL product or pending organization.', 1;

DECLARE @status uniqueidentifier = COALESCE(
    (SELECT TOP 1 Id FROM TicketStatuses WHERE Name = 'Open'),
    (SELECT TOP 1 Id FROM TicketStatuses ORDER BY Name));
DECLARE @priority uniqueidentifier = (SELECT TOP 1 Id FROM TicketPriorities ORDER BY SortOrder);

DECLARE @sender nvarchar(320) = N'offers@leadgen-pro.biz';
DECLARE @customer uniqueidentifier = (SELECT TOP 1 Id FROM Customers WHERE Email = @sender);
IF @customer IS NULL
BEGIN
    SET @customer = NEWID();
    INSERT INTO Customers (Id, Name, Email, CustomerReferenceId, OrganizationId,
                           NotificationPreferences, ContactStatus, ContactSource,
                           IsStakeholder, IsVipUser)
    VALUES (@customer, 'offers', @sender,
            LEFT(UPPER('EMAIL-' + REPLACE(CONVERT(nvarchar(36), NEWID()), '-', '')), 18),
            @org, 'Email', 'Pending', 'Email', 0, 0);
END

DECLARE @mail TABLE (n int IDENTITY, subject nvarchar(300), body nvarchar(max), minutes_ago int);
INSERT INTO @mail (subject, body, minutes_ago) VALUES
(N'10,000 verified B2B leads for your sales team',
 N'Hi there,

We sell verified decision-maker contact lists for Saudi companies: 10,000 emails and phone numbers for only $299. Reply YES to receive a free sample of 100 leads today.

Regards,
LeadGen Pro', 20),
(N'Last chance: 70% off B2B leads',
 N'Hi again,

Our offer ends tonight. 10,000 verified leads for $89 only. Click here to buy now and grow your sales.

LeadGen Pro', 10);

DECLARE @i int = 1, @max int = (SELECT MAX(n) FROM @mail), @made int = 0;
DECLARE @subject nvarchar(300), @body nvarchar(max), @at datetime2, @ticket uniqueidentifier, @ref nvarchar(40);

WHILE @i <= @max
BEGIN
    SELECT @subject = subject, @body = body, @at = DATEADD(minute, -minutes_ago, @now)
    FROM @mail WHERE n = @i;
    IF NOT EXISTS (SELECT 1 FROM Tickets WHERE CustomerId = @customer AND Title = @subject)
    BEGIN
        SET @ticket = NEWID();
        SET @ref = 'T-' + FORMAT(@at, 'yyyyMMddHHmmssfff') + '-' + RIGHT('0000' + CAST(20 + @i AS varchar(4)), 4);
        INSERT INTO Tickets (Id, CustomerId, OrganizationId, ProductId, StatusId, PriorityId,
                             AssignedQueueId, AssignedTeamId, Title, Description, OriginSource,
                             ExternalRefId, RequiresClassification, Type, Scope, Resolution,
                             Impact, Urgency, IsSlaApplicable, IsResponseBreached,
                             IsResolutionBreached, CreatedAt, UpdatedAt, CurrentQueueSince)
        VALUES (@ticket, @customer, @org, @product, @status, @priority,
                NULL, NULL, @subject, @body, 'Email',
                @ref, 1, 'Incident', 'External', 'Pending',
                'User', 'Medium', 0, 0, 0, @at, @at, @at);
        INSERT INTO TicketThreads (Id, TicketId, Channel, MessageBody, IsInternalNote,
                                   SenderType, SenderEmail, EmailSubject, CreatedAt)
        VALUES (NEWID(), @ticket, 'Email', @body, 0, 'Customer', @sender, @subject, @at);
        SET @made += 1;
    END
    SET @i += 1;
END

COMMIT;
SELECT @made AS created,
       (SELECT ContactStatus FROM Customers WHERE Id = @customer) AS contact_status;
'@

$connection = New-Object System.Data.SqlClient.SqlConnection $connectionString
$connection.Open()
try {
    $command = $connection.CreateCommand()
    $command.CommandText = $sql
    $reader = $command.ExecuteReader()
    if ($reader.Read()) {
        Write-Host ("Created {0} spam ticket(s) from offers@leadgen-pro.biz, contact {1}." -f $reader["created"], $reader["contact_status"]) -ForegroundColor Green
    }
    $reader.Close()
}
finally {
    $connection.Close()
}
